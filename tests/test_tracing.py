"""Tracing degrades to a clean no-op when no collector/Arize is configured."""

from observability import tracing


def test_tracing_is_noop_without_collector(monkeypatch):
    import config

    monkeypatch.delenv("ENABLE_TRACING", raising=False)
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    # clear Arize creds too (a real .env may set them) so we test the no-op path
    monkeypatch.setattr(config, "ARIZE_API_KEY", None)
    monkeypatch.setattr(config, "ARIZE_SPACE_ID", None)
    # reset module init state so init_tracing re-evaluates
    tracing._initialized = False
    tracing._tracer = None
    tracing._backend = None

    assert tracing.init_tracing() is False
    assert tracing.is_enabled() is False

    with tracing.span("anything", foo="bar") as s:
        assert s is None  # no-op span yields None

    @tracing.traced("decorated")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5  # decorator is transparent when tracing is off
    assert tracing.current_trace_id() is None
