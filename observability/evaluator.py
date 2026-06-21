"""The self-correction loop — the visible Arize feedback mechanism.

Evals over the Reconciler's risk calls feed back into ONE tunable knob:
``config.RISK_THRESHOLD``. Keeping the loop to a single knob makes the correction
observable and demo-able.

Pattern
-------
1. Run the pipeline on a labelled patient (we know whether they truly warranted
   escalation).
2. ``score_decision`` grades the RiskAssessment against that label.
3. ``adjust_threshold`` nudges the knob: too many false alarms -> raise it,
   missed deterioration -> lower it.

In production the "ground truth" is clinician feedback / readmission outcomes
logged back through Arize; here it's the synthetic patient's known outcome.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from schemas import RiskAssessment


@dataclass
class DecisionEval:
    """One graded decision."""

    correct: bool
    error: float  # |risk_score - ground_truth|, 0..1
    false_positive: bool
    false_negative: bool


def score_decision(assessment: RiskAssessment, ground_truth: bool) -> float:
    """Grade a decision against ground truth: ``1 - |risk - target|`` (1 = perfect)."""
    target = 1.0 if ground_truth else 0.0
    return 1.0 - abs(assessment.risk_score - target)


def evaluate_decision(assessment: RiskAssessment, ground_truth: bool) -> DecisionEval:
    """Richer grade: correctness plus false-positive / false-negative flags."""
    predicted = assessment.recommend_escalation
    return DecisionEval(
        correct=(predicted == ground_truth),
        error=abs(assessment.risk_score - (1.0 if ground_truth else 0.0)),
        false_positive=(predicted and not ground_truth),
        false_negative=(not predicted and ground_truth),
    )


def adjust_threshold(
    assessment: RiskAssessment,
    ground_truth: bool,
    *,
    step: float = 0.05,
) -> float:
    """Nudge the single RISK_THRESHOLD knob based on one decision's error.

    * False positive (escalated a benign case)    -> RAISE threshold (be calmer).
    * False negative (missed a deteriorating case) -> LOWER threshold (be keener).
    * Correct call -> leave it.

    Returns the new threshold. The only place outside config that moves the knob.
    """
    ev = evaluate_decision(assessment, ground_truth)
    current = config.get_risk_threshold()
    if ev.false_positive:
        return config.set_risk_threshold(current + step)
    if ev.false_negative:
        return config.set_risk_threshold(current - step)
    return current


def batch_adjust(
    decisions: list[tuple[RiskAssessment, bool]],
    *,
    step: float = 0.05,
) -> float:
    """Run the correction loop over a batch of (assessment, ground_truth) pairs.

    TODO(observability-owner): replace the per-decision nudge with a proper
    calibration pass (pick the threshold that maximizes balanced accuracy over the
    batch) and log the chosen value + eval metrics to Arize as a dataset/span.
    """
    for assessment, truth in decisions:
        adjust_threshold(assessment, truth, step=step)
    return config.get_risk_threshold()
