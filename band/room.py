"""BandRoom — the governed coordination room's core logic.

Two responsibilities:
  * ``deliberate`` — run ONE bounded round of Skeptic -> Reconciler (no loops),
    audit each step, and return the Reconciler's calibrated RiskAssessment.
  * ``escalation_gate`` — the human-in-the-loop authority check deciding whether
    the system may actually act on a "recommend escalation", written to audit.

Adapter note: dev's skeptic/reconciler are plain (synchronous) functions with
bespoke signatures. Rather than couple BandRoom to those signatures, the caller
passes pre-bound callables (``skeptic_call`` / ``reconciler_call``). BandRoom owns
the *governance* — ordering, the single bounded round, audit, and the authority
gate — and stays agnostic to how the agents are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import config
from band.audit import AuditLog
from schemas import (
    GateDecision,
    RiskAssessment,
    rebuttal_from_skeptic,
    risk_from_reconciler,
)

#: An authority check: given the assessment, may the system act? True grants.
Authorizer = Callable[[RiskAssessment], bool]


def _default_authorizer(_assessment: RiskAssessment) -> bool:
    """Demo authority: a configured care-coordinator authority is present.

    TODO(governance-owner): replace with a real human-in-the-loop check — verify
    the on-call coordinator's Band-issued authority token/signature before the
    system is permitted to escalate.
    """
    return True


@dataclass
class DeliberationResult:
    """What ``deliberate`` returns: the typed assessment plus the raw agent dicts
    (the raw dicts still feed dev's existing UI/log)."""

    assessment: RiskAssessment
    skeptic_out: dict
    reconciler_out: dict


class BandRoom:
    """Core governed deliberation + escalation gate."""

    def __init__(
        self,
        audit: AuditLog | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        # NB: an empty AuditLog is falsy (__len__ == 0), so use an explicit None
        # check — `audit or AuditLog()` would silently drop a passed-in empty log.
        self.audit = audit if audit is not None else AuditLog()
        self.authorizer = authorizer or _default_authorizer

    def deliberate(
        self,
        *,
        patient_id: str,
        analysis_summary: dict,
        skeptic_call: Callable[[], dict],
        reconciler_call: Callable[[dict], dict],
        trace_id: str | None = None,
    ) -> DeliberationResult:
        """Run one bounded round of Skeptic -> Reconciler, auditing each step.

        Bounded by design: the Skeptic rebuts once, the Reconciler judges once. No
        iterative loop — predictable latency/cost and a readable audit trail.

        ``analysis_summary`` is a small dict of the upstream findings (severities,
        EWS) recorded at deliberation start for the audit trail.
        """
        self.audit.append(
            "band_room",
            "deliberation_started",
            payload={"patient_id": patient_id, **analysis_summary},
        )

        skeptic_out = skeptic_call()
        rebuttal = rebuttal_from_skeptic(skeptic_out)
        self.audit.append(
            "skeptic_agent", "rebuttal_submitted", payload=rebuttal.model_dump(exclude={"raw"})
        )

        reconciler_out = reconciler_call(skeptic_out)
        assessment = risk_from_reconciler(reconciler_out, trace_id=trace_id)
        self.audit.append(
            "reconciler_agent",
            "assessment_submitted",
            payload={
                "risk_score": assessment.risk_score,
                "risk_level": assessment.risk_level,
                "recommend_escalation": assessment.recommend_escalation,
                "threshold": config.get_risk_threshold(),
            },
        )
        return DeliberationResult(assessment, skeptic_out, reconciler_out)

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        """Human-in-the-loop authority check; emits a GateDecision + audit record.

        The system may only escalate when BOTH the Reconciler recommends it AND a
        verified authority grants it. This is the governance guarantee: no
        autonomous action without authorized human oversight.
        """
        authority_granted = bool(self.authorizer(assessment))
        escalate = assessment.recommend_escalation and authority_granted

        if not assessment.recommend_escalation:
            reason = "Reconciler did not recommend escalation (risk below threshold / Skeptic prevailed)."
        elif not authority_granted:
            reason = "Escalation recommended but authority NOT granted — held for human authorization."
        else:
            reason = "Escalation recommended and authority granted — proceeding to clinician handoff."

        decision = GateDecision(
            escalate=escalate, authority_granted=authority_granted, reason=reason
        )
        self.audit.append(
            "escalation_gate",
            "gate_decision",
            authority_granted=authority_granted,
            payload=decision.model_dump() | {"risk_score": assessment.risk_score},
        )
        return decision
