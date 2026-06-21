"""Fetch.ai uAgents mesh — full 8-stage pipeline.

Architecture
------------
The three parallel workers (Signal, Trend, Self-Report NLP) run as real
concurrent uAgents in a Bureau and message their results to the Coordinator.
The four sequential stages (RAG, Skeptic, Reconciler, Brief) run *inline* on
the Coordinator via asyncio.to_thread — no extra message round-trips.

  [Signal] ──┐
  [Trend]  ──┼──► [Coordinator (inline)] ──► Knowledge ──► Skeptic ──► Reconciler ──► Brief
  [NLP]    ──┘                                   ↓ (fire-and-forget after Brief)
                                           [Escalation uAgent]

Why offline, not Agentverse
----------------------------
Agents are created with no endpoint= and never call Almanac.register(), so the
Bureau acts as a local in-process message router — zero HTTP traffic to
agentverse.ai. Addresses are real ed25519 keypairs (same format as cloud
agents); adding endpoint= + Almanac registration would make them discoverable
globally on Agentverse with no other code changes.

Speed design
------------
* Parallel fan-out via real uAgent messages (Bureau handles concurrency).
* Sequential chain runs in-place on the Coordinator coroutine via
  asyncio.to_thread (blocking LLM calls → thread pool; no message round-trips).
* Escalation is fire-and-forget: done is signalled the moment Brief completes,
  not after waiting for the escalation ack.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

try:
    from uagents import Agent, Bureau, Context, Model

    _UAGENTS_AVAILABLE = True
except Exception:
    _UAGENTS_AVAILABLE = False

    class Model:  # type: ignore
        pass


# ─── Stage registry ───────────────────────────────────────────────────────────

PARALLEL_STAGES   = ("signal", "trend", "self_report")
SEQUENTIAL_STAGES = ("knowledge", "skeptic", "reconciler", "brief")
ALL_WORKER_STAGES = PARALLEL_STAGES + SEQUENTIAL_STAGES

_SEEDS = {
    "signal":      "recovery-monitor-signal-agent",
    "trend":       "recovery-monitor-trend-agent",
    "self_report": "recovery-monitor-selfreport-agent",
    "knowledge":   "recovery-monitor-knowledge-agent",
    "skeptic":     "recovery-monitor-skeptic-agent",
    "reconciler":  "recovery-monitor-reconciler-agent",
    "brief":       "recovery-monitor-brief-agent",
    "escalation":  "recovery-monitor-escalation-agent",
    "coordinator": "recovery-monitor-coordinator",
}

LABELS = {
    "signal":      "Signal Agent",
    "trend":       "Trend Agent",
    "self_report": "Self-Report NLP",
    "knowledge":   "Medical Knowledge (RAG)",
    "skeptic":     "Adversarial Skeptic",
    "reconciler":  "Reconciler / Judge",
    "brief":       "Clinical Brief (SBAR)",
    "escalation":  "Escalation (Fetch)",
}


# ─── Message models ───────────────────────────────────────────────────────────

class TriggerStage(Model):
    """Coordinator → parallel worker: fire your stage now."""
    stage: str


class StageResult(Model):
    """Parallel worker → coordinator: here is the output."""
    stage: str
    elapsed: float
    payload_json: str


class EscalationHandoff(Model):
    """Coordinator → escalation uAgent: fire-and-forget handoff delivery."""
    patient_id: str
    risk_score: int
    sbar: str
    authority_granted: bool


# ─── Shared per-run context ───────────────────────────────────────────────────

_CTX: dict[str, Any] = {}


# ─── Stage body dispatcher ────────────────────────────────────────────────────

def _call_stage(stage: str):
    """Call the right (blocking) agent function using accumulated results."""
    import agents as dev

    pd           = _CTX["patient_data"]
    client       = _CTX["client"]
    web_research = _CTX.get("web_research")
    results      = _CTX["results"]

    profile = pd["profile"]
    history = pd["sensor_history"]
    reports = pd["self_reports"]
    current = history[-1]
    days    = len(history)

    if stage == "signal":
        return dev.run_signal_agent(current, profile, client)
    if stage == "trend":
        return dev.run_trend_agent(history, profile, client)
    if stage == "self_report":
        return dev.run_self_report_agent(reports, client)
    if stage == "knowledge":
        return dev.run_knowledge_agent(
            results["signal"], results["trend"], results["self_report"],
            client, web_research, profile,
        )
    if stage == "skeptic":
        return dev.run_skeptic_agent(
            results["signal"], results["trend"], results["self_report"],
            results["knowledge"], profile, client,
        )
    if stage == "reconciler":
        return dev.run_reconciler_agent(
            results["signal"], results["trend"], results["self_report"],
            results["knowledge"], results["skeptic"], profile, days, client,
        )
    if stage == "brief":
        return dev.run_brief_agent(results["reconciler"], profile, days, client, web_research)
    raise ValueError(f"unknown stage {stage!r}")


# ─── Parallel worker factory ──────────────────────────────────────────────────

def _make_parallel_worker(stage: str) -> "Agent":
    """A worker that fires on TriggerStage, runs its body, sends StageResult back."""
    agent = Agent(name=f"pipeline-{stage}", seed=_SEEDS[stage])

    @agent.on_message(model=TriggerStage)
    async def _run(ctx: "Context", sender: str, msg: TriggerStage):
        on_start = _CTX["callbacks"].get("on_start")
        if on_start:
            on_start(stage, LABELS[stage])
        t0     = time.time()
        result = await asyncio.to_thread(_call_stage, stage)
        elapsed = round(time.time() - t0, 1)
        await ctx.send(
            _CTX["coordinator_address"],
            StageResult(stage=stage, elapsed=elapsed,
                        payload_json=json.dumps(result, default=str)),
        )

    return agent


# ─── Escalation worker (fire-and-forget recipient) ────────────────────────────

def _make_escalation_worker() -> "Agent":
    """Real Fetch escalation uAgent — logs receipt; does NOT block pipeline done."""
    agent = Agent(name="pipeline-escalation", seed=_SEEDS["escalation"])

    @agent.on_message(model=EscalationHandoff)
    async def _deliver(ctx: "Context", sender: str, msg: EscalationHandoff):
        _CTX["audit"].append(
            "escalation_agent", "handoff_delivered",
            authority_granted=msg.authority_granted,
            payload={
                "via":        "fetch.uagent",
                "address":    ctx.address,
                "patient_id": msg.patient_id,
                "risk_score": msg.risk_score,
            },
        )
        # Pipeline is already done at this point (fire-and-forget design)

    return agent


# ─── Coordinator ──────────────────────────────────────────────────────────────

def _make_coordinator() -> "Agent":
    """Coordinates the full pipeline:

    1. On startup: fires the 3 parallel workers via uAgent messages.
    2. On StageResult (×3): collects parallel outputs; when all 3 arrive,
       runs the sequential chain inline via asyncio.to_thread (no extra messages).
    3. After Brief: fires escalation uAgent (fire-and-forget) then sets done.
    """
    agent = Agent(name="coordinator", seed=_SEEDS["coordinator"])

    # ── helpers (inline, share _CTX) ─────────────────────────────────────────

    def _record(stage: str, result, elapsed: float):
        import agents as dev
        _CTX["results"][stage] = result
        brief = dev.extract_brief(stage, result)
        _CTX["full_log"]["agents"][stage] = {
            "label":           LABELS.get(stage, stage),
            "elapsed_seconds": elapsed,
            "brief":           brief,
            "full_output":     result if not isinstance(result, str) else {"text": result},
        }
        cb = _CTX["callbacks"].get("on_complete")
        if cb:
            cb(stage, LABELS[stage], brief, elapsed)

    def _compute_gate(reconciler_result: dict):
        from schemas import risk_from_reconciler, GateDecision
        assessment = risk_from_reconciler(reconciler_result, trace_id=_CTX.get("trace_id"))
        _CTX["assessment"] = assessment
        _CTX["audit"].append(
            "reconciler_agent", "assessment_submitted",
            payload={
                "risk_score":           assessment.risk_score,
                "risk_level":           assessment.risk_level,
                "recommend_escalation": assessment.recommend_escalation,
            },
        )
        authority_granted = True
        escalate = assessment.recommend_escalation and authority_granted
        reason   = (
            "Escalation recommended — handing off to clinical team."
            if escalate else
            "Risk below threshold — monitoring continues."
        )
        gate = GateDecision(escalate=escalate, authority_granted=authority_granted, reason=reason)
        _CTX["gate"] = gate
        _CTX["audit"].append(
            "escalation_gate", "gate_decision",
            authority_granted=authority_granted,
            payload=gate.model_dump() | {"risk_score": assessment.risk_score},
        )

    # ── sequential chain (runs inline on the coordinator coroutine) ───────────

    async def _run_sequential(ctx: "Context"):
        for stage in SEQUENTIAL_STAGES:
            on_start = _CTX["callbacks"].get("on_start")
            if on_start:
                on_start(stage, LABELS[stage])
            t0      = time.time()
            result  = await asyncio.to_thread(_call_stage, stage)
            elapsed = round(time.time() - t0, 1)
            _record(stage, result, elapsed)
            if stage == "reconciler":
                _compute_gate(result)

        # ── escalation: fire-and-forget, then we're done immediately ──────────
        gate       = _CTX.get("gate")
        assessment = _CTX.get("assessment")
        on_start   = _CTX["callbacks"].get("on_start")
        on_done    = _CTX["callbacks"].get("on_complete")
        esc_addr   = Agent(name="pipeline-escalation", seed=_SEEDS["escalation"]).address

        if on_start:
            on_start("escalation", LABELS["escalation"])

        if gate and gate.escalate:
            patient_id = _CTX["patient_data"]["profile"].get("id", "unknown")
            await ctx.send(
                _CTX["worker_addresses"]["escalation"],
                EscalationHandoff(
                    patient_id=patient_id,
                    risk_score=assessment.risk_score_100,
                    sbar=str(_CTX["results"].get("brief", "")),
                    authority_granted=True,
                ),
            )
            _CTX["audit"].append(
                "escalation_agent", "handoff_sent",
                authority_granted=True,
                payload={"via": "fetch.uagent", "address": esc_addr},
            )
            esc_brief = {
                "escalated":         True,
                "authority_granted": True,
                "reason":            gate.reason,
                "uagent_address":    esc_addr,
            }
        else:
            _CTX["audit"].append(
                "escalation_agent", "escalation_held",
                payload={"reason": gate.reason if gate else "below threshold"},
            )
            esc_brief = {
                "escalated":         False,
                "authority_granted": False,
                "reason":            gate.reason if gate else "No gate decision",
                "uagent_address":    esc_addr,
            }

        _CTX["full_log"]["agents"]["escalation"] = {
            "label":           LABELS["escalation"],
            "elapsed_seconds": 0,
            "brief":           esc_brief,
            "full_output":     esc_brief,
        }
        if on_done:
            on_done("escalation", LABELS["escalation"], esc_brief, 0)

        _CTX["audit"].append("coordinator", "pipeline_complete")
        _CTX["done"].set()   # ← done immediately; escalation worker logs in background

    # ── event handlers ────────────────────────────────────────────────────────

    @agent.on_event("startup")
    async def _start(ctx: "Context"):
        for stage in PARALLEL_STAGES:
            await ctx.send(_CTX["worker_addresses"][stage], TriggerStage(stage=stage))

    @agent.on_message(model=StageResult)
    async def _collect(ctx: "Context", sender: str, msg: StageResult):
        result = json.loads(msg.payload_json)
        _record(msg.stage, result, msg.elapsed)

        if (all(s in _CTX["results"] for s in PARALLEL_STAGES)
                and not _CTX.get("_seq_started")):
            _CTX["_seq_started"] = True
            _CTX["audit"].append("coordinator", "parallel_complete",
                                 payload={"stages": list(PARALLEL_STAGES)})
            asyncio.ensure_future(_run_sequential(ctx))

    return agent


# ─── Public API ───────────────────────────────────────────────────────────────

def is_available() -> bool:
    return _UAGENTS_AVAILABLE


def mesh_status() -> list[dict]:
    """Return every agent's deterministic address (no network needed)."""
    if not _UAGENTS_AVAILABLE:
        return []
    _ensure_loop()
    out = []
    for stage in ALL_WORKER_STAGES:
        a = Agent(name=f"pipeline-{stage}", seed=_SEEDS[stage])
        out.append({"stage": stage, "name": LABELS.get(stage, stage), "address": a.address})
    out.append({"stage": "escalation",  "name": LABELS["escalation"],
                "address": Agent(name="pipeline-escalation",  seed=_SEEDS["escalation"]).address})
    out.append({"stage": "coordinator", "name": "Coordinator",
                "address": Agent(name="coordinator",          seed=_SEEDS["coordinator"]).address})
    return out


def run_full_pipeline_via_mesh(
    patient_data: dict,
    client,
    callbacks: dict | None = None,
    web_research: dict | None = None,
    *,
    timeout: float = 600.0,
) -> dict:
    """Run the 8-stage pipeline via Fetch.ai Bureau.

    Parallel stages run as real concurrent uAgents.
    Sequential stages run inline on the Coordinator coroutine (fast, no round-trips).
    Escalation is fire-and-forget (done is set the moment Brief completes).

    Returns:
      results          — {stage: agent_output_dict}
      full_log         — ready to merge into pipeline JSON log
      audit            — AuditLog instance
      assessment       — typed RiskAssessment (or None)
      gate             — typed GateDecision (or None)
      mesh_addresses   — list of all agent addresses
    """
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed — pip install -r requirements.txt")

    from band.audit import AuditLog

    global _CTX
    _CTX = {
        "client":          client,
        "patient_data":    patient_data,
        "web_research":    web_research,
        "callbacks":       callbacks or {},
        "coordinator_address": None,
        "worker_addresses":    {},
        "results":         {},
        "done":            threading.Event(),
        "audit":           AuditLog(echo=False),
        "assessment":      None,
        "gate":            None,
        "_seq_started":    False,
        "full_log":        {"agents": {}},
        "trace_id":        None,
    }
    error: dict = {}

    def _run_bureau():
        _ensure_loop()
        try:
            coordinator  = _make_coordinator()
            esc_worker   = _make_escalation_worker()
            par_workers  = {s: _make_parallel_worker(s) for s in PARALLEL_STAGES}

            _CTX["coordinator_address"] = coordinator.address
            _CTX["worker_addresses"]    = {s: w.address for s, w in par_workers.items()}
            _CTX["worker_addresses"]["escalation"] = esc_worker.address

            bureau = Bureau(port=_free_port())
            bureau.add(coordinator)
            bureau.add(esc_worker)
            for w in par_workers.values():
                bureau.add(w)
            bureau.run()
        except Exception as exc:
            error["exc"] = exc
            _CTX["done"].set()

    threading.Thread(target=_run_bureau, daemon=True).start()

    if not _CTX["done"].wait(timeout):
        raise TimeoutError("Fetch.ai pipeline timed out.")
    if error.get("exc"):
        raise error["exc"]

    return {
        "results":           dict(_CTX["results"]),
        "full_log":          _CTX["full_log"],
        "audit":             _CTX["audit"],
        "assessment":        _CTX["assessment"],
        "gate":              _CTX.get("gate"),
        "mesh_addresses":    mesh_status(),
    }


# ─── Backward-compat: parallel-only path (CLI / tests) ────────────────────────

def run_parallel_analysis_via_mesh(
    patient_data: dict,
    client,
    callbacks: dict | None = None,
    *,
    timeout: float = 240.0,
) -> dict:
    """Run only the 3 parallel analysis agents (legacy CLI path)."""
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed.")

    global _CTX
    _CTX = {
        "client":          client,
        "patient_data":    patient_data,
        "web_research":    None,
        "callbacks":       callbacks or {},
        "coordinator_address": None,
        "worker_addresses":    {},
        "results":         {},
        "done":            threading.Event(),
        "full_log":        {"agents": {}},
    }
    error: dict = {}

    def _run():
        _ensure_loop()
        try:
            coord   = _make_parallel_only_coordinator(len(PARALLEL_STAGES))
            workers = [_make_parallel_worker(s) for s in PARALLEL_STAGES]
            _CTX["coordinator_address"] = coord.address
            _CTX["worker_addresses"]    = {s: w.address for s, w in zip(PARALLEL_STAGES, workers)}
            bureau = Bureau(port=_free_port())
            bureau.add(coord)
            for w in workers:
                bureau.add(w)
            bureau.run()
        except Exception as exc:
            error["exc"] = exc
            _CTX["done"].set()

    threading.Thread(target=_run, daemon=True).start()
    if not _CTX["done"].wait(timeout):
        raise TimeoutError("Fetch.ai parallel analysis timed out.")
    if error.get("exc"):
        raise error["exc"]
    return dict(_CTX["results"])


def _make_parallel_only_coordinator(expected: int) -> "Agent":
    agent = Agent(name="coordinator", seed=_SEEDS["coordinator"])

    @agent.on_event("startup")
    async def _start(ctx: "Context"):
        for stage in PARALLEL_STAGES:
            await ctx.send(_CTX["worker_addresses"][stage], TriggerStage(stage=stage))

    @agent.on_message(model=StageResult)
    async def _collect(ctx: "Context", sender: str, msg: StageResult):
        import agents as dev
        result = json.loads(msg.payload_json)
        _CTX["results"][msg.stage] = result
        cb = _CTX["callbacks"].get("on_complete")
        if cb:
            cb(msg.stage, LABELS[msg.stage], dev.extract_brief(msg.stage, result), msg.elapsed)
        if len(_CTX["results"]) >= expected:
            _CTX["done"].set()

    return agent


def escalation_uagent() -> "Agent":
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed.")
    return Agent(name="pipeline-escalation", seed=_SEEDS["escalation"])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ─── CLI demo ─────────────────────────────────────────────────────────────────

def _demo() -> int:
    import config
    from patient_data import PATIENTS

    if not _UAGENTS_AVAILABLE:
        print("uagents not installed. `pip install -r requirements.txt`.")
        return 1

    print("Fetch.ai mesh — all agent addresses:")
    for s in mesh_status():
        print(f"  {s['name']:32} {s['address']}")

    client  = config.get_anthropic_client()
    patient = PATIENTS["PT-7421"]
    print(f"\nRunning pipeline for {patient['profile']['name']} via Fetch.ai mesh…\n")

    def on_start(stage, label):
        print(f"  → {label}")

    def on_complete(stage, label, brief, elapsed):
        print(f"  ✓ {label} ({elapsed}s)")

    result = run_full_pipeline_via_mesh(
        patient, client,
        callbacks={"on_start": on_start, "on_complete": on_complete},
    )
    print(f"\nDone. Gate: {result['gate']}")
    print(f"Audit: {len(result['audit'])} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
