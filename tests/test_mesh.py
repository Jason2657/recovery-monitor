"""Fetch.ai mesh: the analysis agents are real uAgents with real addresses."""

import pytest

from mesh import fetch_mesh


def test_mesh_status_exposes_real_uagent_addresses():
    if not fetch_mesh.is_available():
        pytest.skip("uagents not installed")
    statuses = fetch_mesh.mesh_status()
    stages = {s["stage"] for s in statuses}
    # the 3 independent analysis workers are always present (mesh may expose more stages)
    assert {"signal", "trend", "self_report"} <= stages
    for s in statuses:
        assert s["address"]  # deterministic uAgent address derived from the seed
        assert s["address"].startswith("agent1")  # uAgents address prefix


def test_escalation_uagent_builds():
    if not fetch_mesh.is_available():
        pytest.skip("uagents not installed")
    agent = fetch_mesh.escalation_uagent()
    assert agent.address
