"""Pydantic v2 models for the governed (Band) boundary.

dev's analysis agents speak plain dicts, and that's fine — they're fast to write
and the web UI consumes dicts. But the *governed* part of the pipeline (the Band
room: deliberation -> risk -> gate -> audit) is exactly where we want typed,
validated objects, because that's the decision that loops in a human. So these
models wrap the boundary between dev's dict world and the governance layer.

``risk_from_reconciler`` is the single adapter that turns the Reconciler's dict
output into a typed :class:`RiskAssessment`, applying the ``RISK_THRESHOLD`` knob.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

import config


class SkepticRebuttal(BaseModel):
    """The Skeptic's case AGAINST escalation (wraps the skeptic agent dict)."""

    counterpoints: list[str] = Field(default_factory=list)
    suppression_strength: float = Field(
        default=0.0, ge=0.0, le=1.0, description="How hard the rebuttal argues risk down (0..1)."
    )
    raw: dict = Field(default_factory=dict, description="Full skeptic agent output.")


class RiskAssessment(BaseModel):
    """The Reconciler/Judge's calibrated output — what the gate acts on."""

    risk_score: float = Field(ge=0.0, le=1.0, description="Normalized 0..1 risk.")
    risk_score_100: int = Field(ge=0, le=100, description="Reconciler's raw 0..100 score.")
    risk_level: str = "unknown"
    recommend_escalation: bool = False
    rationale: list[str] = Field(default_factory=list)
    recommended_action: str = ""
    time_sensitivity: str = ""
    confidence: str = ""
    trace_id: str | None = Field(default=None, description="Arize/Phoenix trace id, if tracing is on.")
    raw: dict = Field(default_factory=dict, description="Full reconciler agent output.")


class GateDecision(BaseModel):
    """Result of the human-in-the-loop escalation gate."""

    escalate: bool
    authority_granted: bool = Field(description="Did a verified authority approve acting?")
    reason: str


class SBARBrief(BaseModel):
    """Clinician handoff in SBAR format. dev produces a single markdown string;
    we keep both the structured fields (when parseable) and the raw text."""

    text: str = ""
    situation: str = ""
    background: str = ""
    assessment: str = ""
    recommendation: str = ""


class AuditRecord(BaseModel):
    """One append-only entry in the Band room's audit trail."""

    actor: str
    action: str
    authority_granted: bool = False
    timestamp: datetime
    payload: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Adapters: dev's dicts -> typed governance objects
# --------------------------------------------------------------------------- #
def risk_from_reconciler(reconciler_out: dict, *, trace_id: str | None = None) -> RiskAssessment:
    """Build a typed RiskAssessment from dev's reconciler dict, applying the knob.

    The Reconciler returns a 0..100 ``risk_score``; we normalize to 0..1 and set
    ``recommend_escalation`` against the live ``RISK_THRESHOLD`` so the Arize
    correction loop has a real lever on the decision.
    """
    raw_score = reconciler_out.get("risk_score", 0)
    try:
        score_100 = int(round(float(raw_score)))
    except (TypeError, ValueError):
        score_100 = 0
    score_100 = max(0, min(100, score_100))
    normalized = score_100 / 100.0

    drivers = reconciler_out.get("primary_drivers") or []
    rationale = list(drivers)
    if reconciler_out.get("rationale"):
        rationale.append(str(reconciler_out["rationale"]))

    return RiskAssessment(
        risk_score=normalized,
        risk_score_100=score_100,
        risk_level=str(reconciler_out.get("risk_level", "unknown")),
        recommend_escalation=normalized >= config.get_risk_threshold(),
        rationale=rationale,
        recommended_action=str(reconciler_out.get("recommended_action", "")),
        time_sensitivity=str(reconciler_out.get("time_sensitivity", "")),
        confidence=str(reconciler_out.get("confidence", "")),
        trace_id=trace_id,
        raw=reconciler_out,
    )


def rebuttal_from_skeptic(skeptic_out: dict) -> SkepticRebuttal:
    """Build a typed SkepticRebuttal from dev's skeptic dict (new debate schema + old format)."""
    # New schema: debate[].hypothesis; old fallback: counterarguments[].benign_explanation
    debate = skeptic_out.get("debate") or []
    counterpoints = [r.get("hypothesis", "") for r in debate if isinstance(r, dict)]
    if not counterpoints:
        counterpoints = [
            c.get("benign_explanation", "")
            for c in (skeptic_out.get("counterarguments") or [])
            if isinstance(c, dict)
        ]
    if skeptic_out.get("strongest_hypothesis"):
        counterpoints.insert(0, skeptic_out["strongest_hypothesis"])
    confidence = str(skeptic_out.get("skeptic_confidence", "")).lower()
    strength = {"high": 0.8, "moderate": 0.5, "low": 0.2}.get(confidence, 0.3)
    return SkepticRebuttal(
        counterpoints=[c for c in counterpoints if c],
        suppression_strength=strength,
        raw=skeptic_out,
    )
