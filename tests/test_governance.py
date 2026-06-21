"""Band governance: audit trail, the escalation gate, and the bounded round."""

import config
from band.adapter import LocalBandRoom
from band.audit import AuditLog
from schemas import risk_from_reconciler


def setup_function(_):
    config.set_risk_threshold(0.5)  # isolate tests from the correction loop


def test_audit_is_append_only_and_records():
    log = AuditLog()
    log.append("reconciler_agent", "assessment_submitted", payload={"risk": 0.7})
    log.append("escalation_gate", "gate_decision", authority_granted=True)
    assert len(log) == 2
    assert log.records[0].actor == "reconciler_agent"
    assert log.records[1].authority_granted is True
    # records property returns a copy — the trail stays append-only
    log.records.append("nope")
    assert len(log) == 2


def test_risk_from_reconciler_applies_threshold():
    high = risk_from_reconciler({"risk_score": 80})
    low = risk_from_reconciler({"risk_score": 20})
    assert high.risk_score == 0.8 and high.recommend_escalation is True
    assert low.risk_score == 0.2 and low.recommend_escalation is False


def test_gate_holds_below_threshold():
    room = LocalBandRoom()
    decision = room.escalation_gate(risk_from_reconciler({"risk_score": 20}))
    assert decision.escalate is False
    assert decision.authority_granted is True  # authority present, but risk too low


def test_gate_escalates_with_authority():
    room = LocalBandRoom()
    decision = room.escalation_gate(risk_from_reconciler({"risk_score": 90}))
    assert decision.escalate is True
    assert decision.authority_granted is True


def test_gate_blocks_without_authority():
    room = LocalBandRoom(authorizer=lambda _a: False)
    decision = room.escalation_gate(risk_from_reconciler({"risk_score": 90}))
    assert decision.escalate is False  # recommended, but authority NOT granted
    assert decision.authority_granted is False


def test_deliberate_is_one_bounded_round_and_audited():
    room = LocalBandRoom()
    order = []

    def skeptic_call():
        order.append("skeptic")
        return {"skeptic_confidence": "low", "counterarguments": [{"benign_explanation": "caffeine"}]}

    def reconciler_call(skeptic_out):
        order.append("reconciler")
        assert "skeptic" in order  # reconciler sees the skeptic's rebuttal first
        return {"risk_score": 70, "risk_level": "high"}

    result = room.deliberate(
        patient_id="p1", analysis_summary={"severities": {}},
        skeptic_call=skeptic_call, reconciler_call=reconciler_call,
    )
    assert order == ["skeptic", "reconciler"]  # exactly one bounded round
    assert result.assessment.risk_score == 0.7
    actions = [r.action for r in room.audit.records]
    assert actions == ["deliberation_started", "rebuttal_submitted", "assessment_submitted"]
