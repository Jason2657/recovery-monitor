"""Reconciler / Judge agent — weighs evidence vs. the Skeptic's counterargument
and produces a calibrated risk + a bullet-point reasoning trace.

Owner: Person B (Debate & governance).

This is the decision-maker inside the Band room. It reads the analysis findings
and the early-warning score, discounts them by the Skeptic's rebuttal, and emits
a :class:`~recovery_monitor.schemas.RiskAssessment`. It is one of the three
Claude-backed agents; ``recommend_escalation`` is gated on the single tunable
``RISK_THRESHOLD`` knob so the self-correction loop can move the boundary.
"""

from __future__ import annotations

from recovery_monitor import config
from recovery_monitor.observability.tracing import current_trace_id, traced
from recovery_monitor.schemas import (
    AgentFinding,
    EarlyWarningScore,
    RiskAssessment,
    SkepticRebuttal,
)

# Maps the EWS band to a baseline risk contribution.
_BAND_RISK = {"low": 0.15, "medium": 0.5, "high": 0.85}


class ReconcilerAgent:
    """Calibrates a final risk score from findings, EWS, and the rebuttal."""

    name = "reconciler_agent"

    @traced("reconciler_agent.assess")
    async def assess(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        rebuttal: SkepticRebuttal,
    ) -> RiskAssessment:
        """Produce a calibrated risk assessment.

        STUB: a deterministic, explainable blend so the pipeline behaves sensibly
        for both demo profiles. Risk = a mix of the agents' peak/mean severity and
        the EWS band, then discounted by the Skeptic's suppression_strength.

        TODO(governance-owner): replace the arithmetic with a Claude call that
        actually reasons over the findings + rebuttal and returns calibrated risk
        with a genuine reasoning trace. This is the natural home for the
        ``config.get_anthropic_client()`` call. Keep the threshold gating below so
        the evaluator's correction loop still owns the boundary.
        """
        severities = [f.severity for f in findings] or [0.0]
        peak = max(severities)
        mean = sum(severities) / len(severities)
        band_risk = _BAND_RISK.get(ews.band, 0.3)

        # Blend the agents' peak severity with the early-warning band, then let
        # the Skeptic suppress part of it. The band is weighted to dominate while
        # the stub agents return flat severities; rebalance toward `peak` once the
        # analysis agents produce real, differentiated severities.
        raw_risk = 0.35 * peak + 0.65 * band_risk
        risk_score = max(0.0, min(1.0, raw_risk * (1.0 - 0.5 * rebuttal.suppression_strength)))

        threshold = config.get_risk_threshold()
        recommend = risk_score >= threshold

        rationale = [
            f"Early-warning score {ews.score} (band={ews.band}); components={ews.components}.",
            f"Agent severities: peak={peak:.2f}, mean={mean:.2f} "
            f"({', '.join(f'{f.agent_name}={f.severity:.2f}' for f in findings)}).",
            f"Skeptic suppression={rebuttal.suppression_strength:.2f}: "
            f"{'; '.join(rebuttal.counterpoints) or 'no counterpoints'}.",
            f"Calibrated risk={risk_score:.2f} vs. threshold={threshold:.2f} "
            f"-> {'ESCALATE' if recommend else 'hold'}.",
        ]

        return RiskAssessment(
            risk_score=risk_score,
            recommend_escalation=recommend,
            rationale=rationale,
            trace_id=current_trace_id(),
        )
