"""Arize / Phoenix tracing — the boundary every pipeline step runs inside.

``init_tracing()`` is called once at the top of the demo. It tries to stand up an
OpenTelemetry tracer wired to Phoenix/Arize. If the optional deps or the env keys
are missing, it degrades to a NO-OP so the repo runs end-to-end without an Arize
account.

Use either:
    @traced("signal_agent.evaluate")      # decorator (async or sync)
    async def evaluate(...): ...

or:
    with span("band.deliberate") as s:    # context manager
        ...

Both return/yield gracefully whether or not tracing is actually enabled.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from typing import Any, Callable, Iterator

from recovery_monitor import config

# Populated by init_tracing(); stays None when tracing is unavailable.
_tracer: Any | None = None
_initialized: bool = False


def init_tracing(service_name: str | None = None) -> bool:
    """Initialize Phoenix/Arize tracing from env. Returns True if enabled.

    Safe to call multiple times. Never raises — on any failure it logs once via
    the returned bool and leaves tracing as a no-op.
    """
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    _initialized = True

    try:
        # Phoenix gives a local UI + collector; register() returns a tracer
        # provider already wired to OTLP. With ARIZE_* keys set you can point
        # the same OTEL exporter at Arize instead.
        from phoenix.otel import register

        provider = register(
            project_name=service_name or config.SERVICE_NAME,
            # Auto-instrument the Anthropic SDK so LLM calls show up as spans.
            auto_instrument=True,
        )
        _tracer = provider.get_tracer(__name__)
        return True
    except Exception:  # noqa: BLE001 - tracing must never break the pipeline
        # No phoenix/openinference installed, or no collector reachable.
        # TODO(observability-owner): when ARIZE_API_KEY/ARIZE_SPACE_ID are set,
        # configure the OTLP exporter to ship spans to Arize directly.
        _tracer = None
        return False


def is_enabled() -> bool:
    """Whether a real tracer is active."""
    return _tracer is not None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Context manager that opens a span if tracing is on, else a no-op.

    Yields the span object (or None). Attribute kwargs are attached when possible.
    """
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            with contextlib.suppress(Exception):
                current.set_attribute(key, value)
        yield current


def current_trace_id() -> str | None:
    """Hex trace id of the active span, or None when tracing is off."""
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return None
    return None


def traced(name: str | None = None) -> Callable:
    """Decorator wrapping a function (sync or async) in a span.

    Example::

        @traced()                       # span name defaults to the qualname
        async def evaluate(self, ctx): ...
    """

    def decorator(func: Callable) -> Callable:
        span_name = name or getattr(func, "__qualname__", func.__name__)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with span(span_name):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator
