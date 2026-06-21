"""Shared pydantic v2 models — THE CONTRACT.

Everything in the pipeline speaks these types. Define/changes here ripple to
every owner, so treat this module as the integration boundary: agree on a field
before you depend on it. Nothing in here imports from the rest of the package,
so it is safe to import from anywhere.

Flow recap (see docs/architecture.md):
    SensorReading + SelfReport  ->  PatientContext  (what every agent receives)
    analysis agents             ->  AgentFinding    (what each returns)
    early_warning tool          ->  EarlyWarningScore
    Skeptic                     ->  SkepticRebuttal
    Reconciler/Judge            ->  RiskAssessment
    escalation gate             ->  GateDecision
    Clinician brief             ->  SBARBrief
    every governed step         ->  AuditRecord
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class SensorReading(BaseModel):
    """One sample from the home recovery sensor kit."""

    timestamp: datetime
    heart_rate: float = Field(description="Beats per minute (resting where possible).")
    mobility_index: float = Field(
        ge=0.0, le=1.0, description="0=immobile, 1=normal daily movement (tilt/step proxy)."
    )
    ambient_light: float = Field(
        ge=0.0, description="Ambient light proxy; high values at night = sleep disruption."
    )
    touch_checkin: bool = Field(description="Did the patient complete the touch check-in?")


class SelfReport(BaseModel):
    """A daily free-text (or transcribed voice) self-report from the patient."""

    timestamp: datetime
    raw_text: str


class PatientContext(BaseModel):
    """Everything an agent needs to evaluate one timestep.

    This is the single object passed to every analysis agent's ``evaluate``.
    """

    patient_id: str
    latest: SensorReading
    rolling_window: list[SensorReading] = Field(
        default_factory=list, description="Recent readings (typ. last 5–7 days) for trend analysis."
    )
    self_reports: list[SelfReport] = Field(
        default_factory=list, description="Self-reports up to and including the current timestep."
    )


# --------------------------------------------------------------------------- #
# Analysis outputs
# --------------------------------------------------------------------------- #
class AgentFinding(BaseModel):
    """What every analysis agent (Signal / Trend / Self-report NLP) returns."""

    agent_name: str
    summary: str = Field(description="One-line, human-readable finding.")
    severity: float = Field(ge=0.0, le=1.0, description="0=nothing, 1=acutely concerning.")
    evidence: dict = Field(
        default_factory=dict, description="Structured backing for the finding (numbers, quotes)."
    )


class EarlyWarningScore(BaseModel):
    """Output of the deterministic early-warning tool (NEWS2-inspired, simplified)."""

    score: int
    components: dict[str, int] = Field(
        default_factory=dict, description="Per-vital sub-scores that sum to ``score``."
    )
    band: Literal["low", "medium", "high"]


# --------------------------------------------------------------------------- #
# Band-room deliberation
# --------------------------------------------------------------------------- #
class SkepticRebuttal(BaseModel):
    """The Skeptic's case AGAINST escalation."""

    counterpoints: list[str] = Field(
        default_factory=list, description="Benign explanations / reasons not to escalate."
    )
    suppression_strength: float = Field(
        ge=0.0, le=1.0, description="How strongly the rebuttal argues down the risk (0–1)."
    )


class RiskAssessment(BaseModel):
    """The Reconciler/Judge's calibrated output — the decision the gate acts on."""

    risk_score: float = Field(ge=0.0, le=1.0)
    recommend_escalation: bool
    rationale: list[str] = Field(
        default_factory=list, description="Bullet-point reasoning trace (evidence vs. counterargument)."
    )
    trace_id: str | None = Field(default=None, description="Arize/Phoenix span id, if tracing is on.")


# --------------------------------------------------------------------------- #
# Gate + outputs
# --------------------------------------------------------------------------- #
class GateDecision(BaseModel):
    """Result of the human-in-the-loop escalation gate."""

    escalate: bool
    authority_granted: bool = Field(description="Did a verified authority approve acting?")
    reason: str


class SBARBrief(BaseModel):
    """Clinician handoff in SBAR format (Situation, Background, Assessment, Recommendation)."""

    situation: str
    background: str
    assessment: str
    recommendation: str


class AuditRecord(BaseModel):
    """One append-only entry in the Band room's audit trail."""

    actor: str
    action: str
    authority_granted: bool
    timestamp: datetime
    payload: dict = Field(default_factory=dict)
