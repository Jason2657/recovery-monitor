"""Fetch.ai uAgents mesh.

The architecture's "parallel analysis" stage is a perfect fit for Fetch.ai: three
*independent* specialized workers (Signal, Trend, Self-report NLP) that each look
at the patient through one lens and message their finding back. This module makes
those agents **real** ``uagents.Agent`` instances, registered on a real
``Bureau``, communicating over real messages.

The agent *bodies* are dev's existing functions in ``agents.py`` (single source of
truth) — here we wrap them as uAgents. The Band room (governance) consumes the
findings the mesh produces; the Escalation uAgent (``escalation_uagent``) is the
real Fetch worker on the output path.

Design notes
------------
* The 3 analysis agents are independent, so they run concurrently in the Bureau
  and push their ``Finding`` to a coordinator — a clean bounded fan-out, no
  chained request/response to get wrong.
* Agents are created offline (no endpoint / no Almanac registration) so the mesh
  runs with no network and no Agentverse account. ``mesh_status()`` exposes each
  worker's real deterministic address (derived from its seed) to prove they are
  genuine uAgents.
* ``run_parallel_analysis_via_mesh`` is used by the CLI / run_demo path. The web
  SSE path calls the same bodies directly for low latency (see agents.py).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

try:
    from uagents import Agent, Bureau, Context, Model

    _UAGENTS_AVAILABLE = True
except Exception:  # pragma: no cover - optional until installed
    _UAGENTS_AVAILABLE = False

    class Model:  # type: ignore - minimal fallback so the module imports
        pass


#: The three independent analysis workers (the "parallel analysis" stage).
ANALYSIS_STAGES = ("signal", "trend", "self_report")

_SEEDS = {
    "signal": "recovery-monitor-signal-agent",
    "trend": "recovery-monitor-trend-agent",
    "self_report": "recovery-monitor-selfreport-agent",
    "coordinator": "recovery-monitor-coordinator",
    "escalation": "recovery-monitor-escalation-agent",
}

_LABELS = {
    "signal": "Signal Agent",
    "trend": "Trend Agent",
    "self_report": "Self-Report NLP",
}


# --------------------------------------------------------------------------- #
# Message models
# --------------------------------------------------------------------------- #
class Finding(Model):
    """A worker's analysis result, sent back to the coordinator.

    uAgents messages must have typed fields; the agent's full dict output is
    JSON-encoded into ``payload_json``.
    """

    stage: str
    elapsed: float
    payload_json: str


class SBARMessage(Model):
    """The clinical handoff the Escalation uAgent delivers to a coordinator."""

    patient_id: str
    risk_score: int
    sbar: str


# --------------------------------------------------------------------------- #
# Per-run context (handlers read from here; set before the Bureau runs)
# --------------------------------------------------------------------------- #
_CTX: dict[str, Any] = {}


def _call_body(stage: str):
    """Invoke dev's agent function for ``stage`` against the current run context.

    Imported lazily to avoid a circular import (agents.py imports this module).
    """
    import agents as dev

    profile = _CTX["patient_data"]["profile"]
    history = _CTX["patient_data"]["sensor_history"]
    reports = _CTX["patient_data"]["self_reports"]
    client = _CTX["client"]
    current = history[-1]

    if stage == "signal":
        return dev.run_signal_agent(current, profile, client)
    if stage == "trend":
        return dev.run_trend_agent(history, profile, client)
    if stage == "self_report":
        return dev.run_self_report_agent(reports, client)
    raise ValueError(f"unknown stage {stage!r}")


def _make_worker(stage: str):
    agent = Agent(name=f"analysis-{stage}", seed=_SEEDS[stage])

    @agent.on_event("startup")
    async def _run(ctx: "Context"):  # noqa: ANN001
        on_start = _CTX["callbacks"].get("on_start")
        if on_start:
            on_start(stage, _LABELS[stage])
        t0 = time.time()
        result = _call_body(stage)
        elapsed = round(time.time() - t0, 1)
        await ctx.send(
            _CTX["coordinator_address"],
            Finding(stage=stage, elapsed=elapsed, payload_json=json.dumps(result, default=str)),
        )

    return agent


def _make_coordinator(expected: int):
    agent = Agent(name="coordinator", seed=_SEEDS["coordinator"])

    @agent.on_message(model=Finding)
    async def _collect(ctx: "Context", sender: str, msg: Finding):  # noqa: ANN001
        result = json.loads(msg.payload_json)
        _CTX["results"][msg.stage] = result
        on_complete = _CTX["callbacks"].get("on_complete")
        if on_complete:
            import agents as dev

            on_complete(msg.stage, _LABELS[msg.stage], dev.extract_brief(msg.stage, result), msg.elapsed)
        if len(_CTX["results"]) >= expected:
            _CTX["done"].set()

    return agent


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_available() -> bool:
    """Whether the uagents dependency is importable."""
    return _UAGENTS_AVAILABLE


def mesh_status() -> list[dict]:
    """Return each analysis worker's real uAgent address (no network needed)."""
    if not _UAGENTS_AVAILABLE:
        return []
    out = []
    for stage in ANALYSIS_STAGES:
        a = Agent(name=f"analysis-{stage}", seed=_SEEDS[stage])
        out.append({"stage": stage, "name": a.name, "address": a.address})
    return out


def run_parallel_analysis_via_mesh(
    patient_data: dict,
    client,
    callbacks: dict | None = None,
    *,
    timeout: float = 240.0,
) -> dict:
    """Run Signal/Trend/Self-report as real uAgents in a Bureau; collect findings.

    Returns ``{stage: agent_dict}`` for the three analysis stages. Raises if
    uagents is unavailable or the round-trip times out (never silently fakes).
    """
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed — cannot run the Fetch.ai mesh.")

    global _CTX
    coordinator = _make_coordinator(len(ANALYSIS_STAGES))
    workers = [_make_worker(s) for s in ANALYSIS_STAGES]

    # Use a non-8000 port so the Bureau never collides with the FastAPI app.
    bureau = Bureau(port=8771)
    bureau.add(coordinator)
    for w in workers:
        bureau.add(w)

    _CTX = {
        "client": client,
        "patient_data": patient_data,
        "callbacks": callbacks or {},
        "coordinator_address": coordinator.address,
        "results": {},
        "done": threading.Event(),
    }

    thread = threading.Thread(target=bureau.run, daemon=True)
    thread.start()

    if not _CTX["done"].wait(timeout):
        raise TimeoutError("Fetch.ai mesh analysis timed out.")
    return dict(_CTX["results"])


def escalation_uagent():
    """The real Fetch.ai uAgent that delivers the SBAR handoff (output path)."""
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed — cannot build the escalation uAgent.")
    return Agent(name="escalation", seed=_SEEDS["escalation"])


def _demo() -> int:
    """`python -m mesh.fetch_mesh` — boot the real Bureau on a demo patient."""
    import config
    from patient_data import PATIENTS

    if not _UAGENTS_AVAILABLE:
        print("uagents not installed. `pip install -r requirements.txt`.")
        return 1

    print("Fetch.ai mesh — real uAgents:")
    for s in mesh_status():
        print(f"  {s['name']:18} {s['address']}")

    client = config.get_anthropic_client()
    patient = PATIENTS["PT-7421"]
    print(f"\nDispatching parallel analysis for {patient['profile']['name']} over the mesh...\n")

    def on_complete(stage, label, brief, elapsed):
        print(f"  ✓ {label} ({elapsed}s) — severity {brief.get('severity', '?')}")

    results = run_parallel_analysis_via_mesh(patient, client, {"on_complete": on_complete})
    print(f"\nCollected {len(results)} findings over real uAgent messages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
