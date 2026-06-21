"""Clinician brief agent — produces the SBAR handoff on the "risk high" fork.

Owner: Person D (Language & handoff).

When the gate escalates, a clinician should get a crisp, structured handoff, not
a data dump. SBAR (Situation, Background, Assessment, Recommendation) is the
standard clinical communication format. This is a Claude-backed agent.
"""

from __future__ import annotations

from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import (
    AgentFinding,
    EarlyWarningScore,
    PatientContext,
    RiskAssessment,
    SBARBrief,
)


class ClinicianBriefAgent:
    """Renders a RiskAssessment + context into an SBAR brief for a clinician."""

    name = "clinician_brief_agent"

    @traced("clinician_brief_agent.build")
    async def build(
        self,
        context: PatientContext,
        findings: list[AgentFinding],
        assessment: RiskAssessment,
        ews: EarlyWarningScore,
    ) -> SBARBrief:
        """Build the SBAR handoff.

        STUB: assembles a serviceable SBAR from the structured inputs so the
        escalation path is demonstrable end-to-end.

        TODO(language-owner): replace with a Claude call that writes a fluent,
        clinician-grade SBAR — tightening the Situation/Background into prose,
        and turning the rationale into a defensible Assessment. Keep it grounded:
        cite the actual numbers/quotes from ``findings`` and ``evidence`` so the
        brief is auditable.
        """
        r = context.latest
        finding_lines = "; ".join(f"{f.agent_name}: {f.summary}" for f in findings)

        return SBARBrief(
            situation=(
                f"Home-recovery patient {context.patient_id}: risk "
                f"{assessment.risk_score:.0%}, early-warning band '{ews.band}' "
                f"(score {ews.score}). System recommends clinician review."
            ),
            background=(
                f"Post-surgery remote monitoring. Latest vitals — HR "
                f"{r.heart_rate:.0f} bpm, mobility {r.mobility_index:.2f}, "
                f"touch check-in {'done' if r.touch_checkin else 'missed'}. "
                f"{len(context.rolling_window)} readings in rolling window; "
                f"{len(context.self_reports)} self-reports on file."
            ),
            assessment=(
                "Reconciler rationale: " + " ".join(assessment.rationale)
                + (f" Agent findings — {finding_lines}." if finding_lines else "")
            ),
            recommendation=(
                "Contact the care coordinator for clinical review of trend and "
                "symptoms; consider a check-in call. Decision-support only — "
                "not a diagnosis."
            ),
        )
