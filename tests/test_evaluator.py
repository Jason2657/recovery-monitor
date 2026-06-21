"""Arize self-correction loop: the single RISK_THRESHOLD knob moves correctly."""

import config
from observability import evaluator
from schemas import risk_from_reconciler


def setup_function(_):
    config.set_risk_threshold(0.5)


def test_false_positive_raises_threshold():
    # predicts escalate (0.9), truth is benign -> false positive -> be calmer
    assessment = risk_from_reconciler({"risk_score": 90})
    new = evaluator.adjust_threshold(assessment, ground_truth=False)
    assert new > 0.5


def test_false_negative_lowers_threshold():
    # predicts hold (0.1), truth is escalate -> false negative -> be keener
    assessment = risk_from_reconciler({"risk_score": 10})
    new = evaluator.adjust_threshold(assessment, ground_truth=True)
    assert new < 0.5


def test_correct_call_leaves_threshold():
    assessment = risk_from_reconciler({"risk_score": 90})
    new = evaluator.adjust_threshold(assessment, ground_truth=True)
    assert new == 0.5


def test_score_decision_rewards_calibration():
    confident_right = risk_from_reconciler({"risk_score": 100})
    assert evaluator.score_decision(confident_right, ground_truth=True) == 1.0
    confident_wrong = risk_from_reconciler({"risk_score": 100})
    assert evaluator.score_decision(confident_wrong, ground_truth=False) == 0.0
