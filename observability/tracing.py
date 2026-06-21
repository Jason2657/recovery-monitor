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
_backend: str | None = None  # "arize" | "phoenix" | None


def _ensure_anthropic_instrumented(provider) -> None:
    """Belt-and-suspenders: explicitly instrument the Anthropic SDK in case
    ``auto_instrument`` didn't pick it up. Safe if already done / package absent."""
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        inst = AnthropicInstrumentor()
        # auto_instrument usually already did this; avoid a double-instrument warning.
        if not getattr(inst, "is_instrumented_by_opentelemetry", False):
            inst.instrument(tracer_provider=provider)
    except Exception:  # noqa: BLE001 - already instrumented, or package missing
        pass


def init_tracing(service_name: str | None = None) -> bool:
    """Initialize Arize/Phoenix tracing from env. Returns True if enabled.

    Backend preference (opt-in — clean no-op when nothing is configured):
      1. **Arize AX cloud** — when ARIZE_API_KEY + ARIZE_SPACE_ID are set; ships
         OpenInference traces to app.arize.com (the booth environment).
      2. **Phoenix** (local ``phoenix serve`` or Phoenix Cloud) — when
         ENABLE_TRACING or PHOENIX_COLLECTOR_ENDPOINT is set.
      3. No-op otherwise.

    Safe to call repeatedly. Never raises — on any failure tracing stays a no-op.
    """
    global _tracer, _initialized, _backend
    if _initialized:
        return _tracer is not None
    _initialized = True

    project = service_name or config.ARIZE_PROJECT_NAME

    # 1) Arize AX cloud — the real sponsor environment.
    if config.ARIZE_API_KEY and config.ARIZE_SPACE_ID:
        try:
            from arize.otel import register as arize_register

            provider = arize_register(
                space_id=config.ARIZE_SPACE_ID,
                api_key=config.ARIZE_API_KEY,
                project_name=project,
                auto_instrument=True,  # picks up openinference-instrumentation-anthropic
            )
            _ensure_anthropic_instrumented(provider)
            _tracer = provider.get_tracer(__name__)
            _backend = "arize"
            return True
        except Exception:  # noqa: BLE001 - never break the pipeline; try Phoenix
            _tracer = None

    # 2) Phoenix (local collector or Phoenix Cloud).
    if os.getenv("ENABLE_TRACING") or os.getenv("PHOENIX_COLLECTOR_ENDPOINT"):
        try:
            from phoenix.otel import register

            kwargs: dict = {"project_name": project, "auto_instrument": True}
            if os.getenv("PHOENIX_API_KEY"):
                kwargs["api_key"] = os.getenv("PHOENIX_API_KEY")
            provider = register(**kwargs)
            _ensure_anthropic_instrumented(provider)
            _tracer = provider.get_tracer(__name__)
            _backend = "phoenix"
            return True
        except Exception:  # noqa: BLE001
            _tracer = None

    return False


def is_enabled() -> bool:
    """Whether a real tracer is active."""
    return _tracer is not None


def backend() -> str | None:
    """Which tracing backend is live: 'arize', 'phoenix', or None."""
    return _backend


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


def current_span_id() -> str | None:
    """Hex span id (16 chars) of the active span — call INSIDE the span you want
    evals attached to (e.g. the pipeline root span). None when tracing is off."""
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.span_id:
            return format(ctx.span_id, "016x")
    except Exception:  # noqa: BLE001
        return None
    return None


def flush(timeout_millis: int = 5000) -> None:
    """Force-export pending spans so evaluations logged right after attach to the
    span reliably. No-op when tracing is off; never raises."""
    if _tracer is None:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=timeout_millis)
    except Exception:  # noqa: BLE001
        pass


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
