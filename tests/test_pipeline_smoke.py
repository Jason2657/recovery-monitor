"""End-to-end smoke test — the day-one integration guarantee.

Asserts the full pipeline runs to completion on the stubbed agent logic for BOTH
profiles. This is the test that proves integration before anyone writes real
agent logic: if it goes red, the wiring (not the cleverness) broke.
"""

from __future__ import annotations

import pytest

from recovery_monitor.data import synthetic
from recovery_monitor.run_demo import run_pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", sorted(synthetic.PROFILE_LABELS))
async def test_pipeline_runs_end_to_end(profile):
    results = await run_pipeline(profile, days=7, speed=0.0, verbose=False)

    # Ran every day and produced a complete, well-formed result each step.
    assert len(results) == 7
    for r in results:
        assert r.ews_band in {"low", "medium", "high"}
        assert 0.0 <= r.risk_score <= 1.0
        assert r.decision.escalate == r.escalated
        assert r.assessment.rationale  # reasoning trace is always populated
        # Governance invariant: escalation requires granted authority.
        assert not (r.escalated and not r.decision.authority_granted)


@pytest.mark.asyncio
async def test_deterioration_escalates_at_least_once():
    """The deteriorating patient should trip the gate by the end of the window."""
    results = await run_pipeline("deterioration", days=7, speed=0.0, verbose=False)
    assert any(r.escalated for r in results)


@pytest.mark.asyncio
async def test_benign_decoy_never_escalates():
    """The Skeptic should suppress the benign decoy's alarming-looking days."""
    results = await run_pipeline("benign_decoy", days=7, speed=0.0, verbose=False)
    assert not any(r.escalated for r in results)
