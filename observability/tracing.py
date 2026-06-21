"""Arize / Phoenix tracing — the boundary every pipeline step runs inside.

``init_tracing()`` is called once at startup. It stands up an OpenTelemetry tracer
wired to Phoenix/Arize and auto-instruments the Anthropic SDK so every Claude call
shows up as a span. If the optional deps or env keys are missing it degrades to a
NO-OP, so the app runs end-to-end without an Arize account.

Use either form:
    @traced("signal_agent")              # decorator (sync or async)
    def run_signal_agent(...): ...

    with span("band.deliberate"):        # context manager
        ...
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import os
from typing import Any, Callable, Iterator

import config

# Populated by init_tracing(); stays None when tracing is unavailable.
_tracer: Any | None = None
_initialized: bool = False


def init_tracing(service_name: str | None = None) -> bool:
    """Initialize Phoenix/Arize tracing from env. Returns True if enabled.

    Safe to call repeatedly. Never raises — on any failure tracing stays a no-op.
    """
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    _initialized = True

    # Opt-in: stay a clean no-op unless a collector/Arize is configured, so the
    # demo never spews connection errors when nothing is listening.
    if not (
        os.getenv("ENABLE_TRACING")
        or os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
        or config.ARIZE_API_KEY
    ):
        _tracer = None
        return False

    try:
        # phoenix.otel.register() returns a provider already wired to an OTLP
        # collector (local Phoenix UI by default). With ARIZE_* keys set, the same
        # OpenInference instrumentation can ship to Arize cloud instead.
        from phoenix.otel import register

        provider = register(
            project_name=service_name or config.SERVICE_NAME,
            auto_instrument=True,  # picks up openinference-instrumentation-anthropic
        )
        _tracer = provider.get_tracer(__name__)
        return True
    except Exception:  # noqa: BLE001 - tracing must NEVER break the pipeline
        # TODO(observability-owner): when ARIZE_API_KEY/ARIZE_SPACE_ID are set,
        # configure the OTLP exporter to ship spans to Arize directly.
        _tracer = None
        return False


def is_enabled() -> bool:
    """Whether a real tracer is active."""
    return _tracer is not None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span if tracing is on, else a no-op. Yields the span (or None)."""
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
    """Decorator wrapping a function (sync or async) in a span."""

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
