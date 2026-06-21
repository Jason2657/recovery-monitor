"""Central configuration: env loading, the Anthropic client factory, and the
single tunable ``RISK_THRESHOLD`` knob that the self-correction loop nudges.

Design notes
------------
* Heavy / optional deps (``anthropic``) are imported lazily inside factories so
  the pipeline imports and the demo run *without* them installed or keyed.
* ``RISK_THRESHOLD`` is read/written through ``get_risk_threshold`` /
  ``set_risk_threshold`` rather than imported by value — that is the ONLY way the
  evaluator's correction loop can move the knob and have every reader see it.
  (``from config import RISK_THRESHOLD`` would freeze the value at import time.)
"""

from __future__ import annotations

import os

# Load .env if python-dotenv is available; harmless no-op otherwise.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dep
    pass


# --------------------------------------------------------------------------- #
# The one tunable knob (see observability/evaluator.py)
# --------------------------------------------------------------------------- #
#: Default escalation threshold. RiskAssessment.recommend_escalation is set when
#: risk_score >= this value. Bounded to [0, 1].
DEFAULT_RISK_THRESHOLD: float = float(os.getenv("RISK_THRESHOLD", "0.5"))

_risk_threshold: float = DEFAULT_RISK_THRESHOLD


def get_risk_threshold() -> float:
    """Current escalation threshold (read this at call time, never cache it)."""
    return _risk_threshold


def set_risk_threshold(value: float) -> float:
    """Set the escalation threshold (clamped to [0, 1]). Returns the new value.

    Only the self-correction loop in ``observability.evaluator`` should call this.
    """
    global _risk_threshold
    _risk_threshold = max(0.0, min(1.0, float(value)))
    return _risk_threshold


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
#: Default Claude model for all LLM calls. Override via ANTHROPIC_MODEL.
#: Standardize on Claude; pick the tier per role if needed (e.g. a stronger
#: model for the Reconciler/Judge), but keep the client centralized here.
DEFAULT_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def get_anthropic_client():
    """Return a configured Anthropic client.

    Imported lazily so the package works without the ``anthropic`` dependency or
    an API key (the stubbed agents never call this). Raises a clear error if used
    without a key once real LLM logic is wired in.
    """
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - optional until LLM logic lands
        raise RuntimeError(
            "anthropic is not installed. Run `uv sync` (or `pip install anthropic`) "
            "to enable LLM calls."
        ) from exc

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return Anthropic(api_key=api_key)


# --------------------------------------------------------------------------- #
# Misc env passthroughs (read on demand by the layers that need them)
# --------------------------------------------------------------------------- #
ARIZE_API_KEY: str | None = os.getenv("ARIZE_API_KEY") or None
ARIZE_SPACE_ID: str | None = os.getenv("ARIZE_SPACE_ID") or None
BAND_API_KEY: str | None = os.getenv("BAND_API_KEY") or None

#: Project name used for the observability service/span resource.
SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "recovery-monitor")
