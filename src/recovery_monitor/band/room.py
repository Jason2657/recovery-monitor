"""BandRoom — the governed coordination room's core logic.

Sponsor boundary: Band is where agents reconcile *under verified authority* and
produce an audit trail. This module holds the governance logic; the backend
choice (in-process vs. the real Band API) lives behind ``band.adapter``.

Two responsibilities:
  * ``deliberate`` — run ONE bounded round of Skeptic -> Reconciler (no loops),
    returning the Reconciler's calibrated RiskAssessment.
  * ``escalation_gate`` — the human-in-the-loop authority check that decides
    whether the system may actually act on a "recommend escalation", and writes
    the decision to the audit trail.

Every consequential step writes an AuditRecord.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from recovery_monitor.agents.reconciler_agent import ReconcilerAgent
from recovery_monitor.agents.skeptic_agent import SkepticAgent
from recovery_monitor.band.audit import AuditLog
from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import (
    AgentFinding,
    EarlyWarningScore,
    GateDecision,
    PatientContext,
    RiskAssessment,
)

#: An authority check: given the assessment, may the system act? Returns True to
#: grant. The default auto-grants (a configured care-coordinator authority) and
#: stands in for a real human-in-the-loop confirmation.
Authorizer = Callable[[RiskAssessment], bool]


def _default_authorizer(_assessment: RiskAssessment) -> bool:
    """Demo authority: a configured care-coordinator authority is present.

    TODO(governance-owner): replace with a real human-in-the-loop check —
    verify the on-call coordinator's Band-issued authority token / signature
    before the system is permitted to escalate.
    """
    return True


class BandRoom:
    """Core governed deliberation + escalation gate."""

    def __init__(
        self,
        reconciler: ReconcilerAgent | None = None,
        skeptic: SkepticAgent | None = None,
        audit: AuditLog | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        self.reconciler = reconciler or ReconcilerAgent()
        self.skeptic = skeptic or SkepticAgent()
        self.audit = audit or AuditLog()
        self.authorizer = authorizer or _default_authorizer

    @traced("band.deliberate")
    async def deliberate(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        context: PatientContext,
    ) -> RiskAssessment:
        """Run one bounded round of Skeptic <-> Reconciler.

        Bounded by design: the Skeptic rebuts once, the Reconciler judges once.
        No iterative loop — keeps latency and cost predictable, and the audit
        trail readable.

        TODO(governance-owner): if you ever add multi-round debate, cap rounds
        explicitly and audit each round; never let it run unbounded.
        """
        self.audit.append(
            "band_room",
            "deliberation_started",
            payload={
                "patient_id": context.patient_id,
                "ews_band": ews.band,
                "ews_score": ews.score,
                "findings": [f.model_dump() for f in findings],
            },
        )

        rebuttal = await self.skeptic.rebut(findings, ews, context)
        self.audit.append(
            self.skeptic.name,
            "rebuttal_submitted",
            payload=rebuttal.model_dump(),
        )

        assessment = await self.reconciler.assess(findings, ews, rebuttal)
        self.audit.append(
            self.reconciler.name,
            "assessment_submitted",
            payload=assessment.model_dump(),
        )
        return assessment

    @traced("band.escalation_gate")
    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        """Human-in-the-loop authority check; emits a GateDecision + audit record.

        The system may only escalate when BOTH the Reconciler recommends it AND a
        verified authority grants it. This is the governance guarantee: no
        autonomous action without authorized human oversight.
        """
        authority_granted = bool(self.authorizer(assessment))
        escalate = assessment.recommend_escalation and authority_granted

        if not assessment.recommend_escalation:
            reason = "Reconciler did not recommend escalation (Skeptic prevailed / risk below threshold)."
        elif not authority_granted:
            reason = "Escalation recommended but authority NOT granted — held for human authorization."
        else:
            reason = "Escalation recommended and authority granted — proceeding to clinician handoff."

        decision = GateDecision(
            escalate=escalate,
            authority_granted=authority_granted,
            reason=reason,
        )
        self.audit.append(
            "escalation_gate",
            "gate_decision",
            authority_granted=authority_granted,
            payload=decision.model_dump() | {"risk_score": assessment.risk_score},
        )
        return decision
