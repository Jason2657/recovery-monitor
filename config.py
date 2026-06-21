"""Central configuration: model selection, the Anthropic client factory, and the
single tunable ``RISK_THRESHOLD`` knob the self-correction loop nudges.

Why this module exists
----------------------
dev's pipeline hardcoded the model and built ad-hoc clients. Centralizing both
here gives us (a) one place to swap models, (b) one client factory the whole app
shares, and (c) the single ``RISK_THRESHOLD`` knob that the Arize self-correction
loop moves. Read the knob through ``get_risk_threshold()`` at call time — never
``from config import RISK_THRESHOLD`` (that would freeze the value at import).
"""

from __future__ import annotations

import os

try:  # load .env if python-dotenv is present; harmless otherwise
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dep
    pass


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
#: Default Claude model for every LLM call. Override with ANTHROPIC_MODEL.
MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")


def get_anthropic_client():
    """Return a configured Anthropic client (lazy import, clear errors)."""
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - optional until LLM logic runs
        raise RuntimeError(
            "anthropic is not installed. `pip install -r requirements.txt`."
        ) from exc

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return Anthropic(api_key=api_key)


# --------------------------------------------------------------------------- #
# The one tunable knob (see observability/evaluator.py)
# --------------------------------------------------------------------------- #
#: Escalation fires when the Reconciler's normalized risk (0..1) >= this value.
DEFAULT_RISK_THRESHOLD: float = float(os.getenv("RISK_THRESHOLD", "0.5"))

_risk_threshold: float = DEFAULT_RISK_THRESHOLD


def get_risk_threshold() -> float:
    """Current escalation threshold (read at call time, never cache)."""
    return _risk_threshold


def set_risk_threshold(value: float) -> float:
    """Set the escalation threshold (clamped to [0, 1]); returns the new value.

    Only the self-correction loop in ``observability.evaluator`` should call this.
    """
    global _risk_threshold
    _risk_threshold = max(0.0, min(1.0, float(value)))
    return _risk_threshold


# --------------------------------------------------------------------------- #
# Env passthroughs (read on demand by the layers that need them)
# --------------------------------------------------------------------------- #
ARIZE_API_KEY: str | None = os.getenv("ARIZE_API_KEY") or None
ARIZE_SPACE_ID: str | None = os.getenv("ARIZE_SPACE_ID") or None
BAND_API_KEY: str | None = os.getenv("BAND_API_KEY") or None
DEEPGRAM_API_KEY: str | None = os.getenv("DEEPGRAM_API_KEY") or None

#: Service/project name used for the observability resource + Phoenix project.
SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "recovery-monitor")
