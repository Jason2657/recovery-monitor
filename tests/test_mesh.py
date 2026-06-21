"""Fetch.ai mesh: the analysis agents are real uAgents with real addresses."""

import pytest

from mesh import fetch_mesh


def test_mesh_status_exposes_real_uagent_addresses():
    if not fetch_mesh.is_available():
        pytest.skip("uagents not installed")
    statuses = fetch_mesh.mesh_status()
    assert {s["stage"] for s in statuses} == {"signal", "trend", "self_report"}
    for s in statuses:
        assert s["address"]  # deterministic uAgent address derived from the seed
        assert s["address"].startswith("agent1")  # uAgents address prefix


def test_escalation_uagent_builds():
    if not fetch_mesh.is_available():
        pytest.skip("uagents not installed")
    agent = fetch_mesh.escalation_uagent()
    assert agent.address
