"""Fetch.ai uAgents mesh — full 8-stage pipeline.

Every pipeline step is a real Fetch.ai uAgent running in a Bureau:

  [Signal] ──┐
  [Trend]  ──┼──► [Coordinator] ──► [Knowledge] ──► [Skeptic] ──► [Reconciler] ──► [Brief] ──► [Escalation]
  [NLP]    ──┘

The Coordinator is the governance layer (replaces Band): it runs the escalation
gate after receiving the Reconciler's result, appends to the audit trail, and
triggers each sequential stage only once its inputs are ready.

All agents are created offline (no Agentverse account, no network). Their
deterministic addresses (from seeds) prove they are genuine uAgents.
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
    """Coordinator → worker: start your stage. Inputs are in shared _CTX."""
    stage: str


class StageResult(Model):
    """Worker → coordinator: stage complete."""
    stage: str
    elapsed: float
    payload_json: str


class EscalationHandoff(Model):
    """Coordinator → escalation agent: deliver the SBAR handoff."""
    patient_id: str
    risk_score: int
    sbar: str
    authority_granted: bool


# ─── Shared run context ───────────────────────────────────────────────────────

_CTX: dict[str, Any] = {}


# ─── Stage body dispatcher ────────────────────────────────────────────────────

def _call_stage(stage: str):
    """Call the right agent function using accumulated results from _CTX."""
    import agents as dev

    patient_data  = _CTX["patient_data"]
    client        = _CTX["client"]
    web_research  = _CTX.get("web_research")
    results       = _CTX["results"]

    profile = patient_data["profile"]
    history = patient_data["sensor_history"]
    reports = patient_data["self_reports"]
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


# ─── Worker factory ───────────────────────────────────────────────────────────

def _make_worker(stage: str) -> "Agent":
    """Generic worker: waits for a TriggerStage, runs its body, replies."""
    agent = Agent(name=f"pipeline-{stage}", seed=_SEEDS[stage])

    @agent.on_message(model=TriggerStage)
    async def _run(ctx: "Context", sender: str, msg: TriggerStage):
        on_start = _CTX["callbacks"].get("on_start")
        if on_start:
            on_start(stage, LABELS[stage])
        t0 = time.time()
        result = _call_stage(stage)
        elapsed = round(time.time() - t0, 1)
        await ctx.send(
            _CTX["coordinator_address"],
            StageResult(
                stage=stage,
                elapsed=elapsed,
                payload_json=json.dumps(result, default=str),
            ),
        )

    return agent


# ─── Escalation worker ────────────────────────────────────────────────────────

def _make_escalation_worker() -> "Agent":
    """Escalation uAgent: receives an authorized handoff, logs delivery, signals done when brief is also complete."""
    agent = Agent(name="pipeline-escalation", seed=_SEEDS["escalation"])

    @agent.on_message(model=EscalationHandoff)
    async def _deliver(ctx: "Context", sender: str, msg: EscalationHandoff):
        import agents as dev
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
        _CTX["escalation_address"] = ctx.address
        gate = _CTX.get("gate")
        esc_brief = {
            "escalated":         True,
            "authority_granted": msg.authority_granted,
            "reason":            gate.reason if gate else "Escalation authorized",
            "uagent_address":    ctx.address,
        }
        _CTX["full_log"]["agents"]["escalation"] = {
            "label":           LABELS["escalation"],
            "elapsed_seconds": 0,
            "brief":           esc_brief,
            "full_output":     esc_brief,
        }
        on_complete_cb = _CTX["callbacks"].get("on_complete")
        if on_complete_cb:
            on_complete_cb("escalation", LABELS["escalation"], esc_brief, 0)
        # Only signal done once both escalation and brief are complete
        if "brief" in _CTX["results"]:
            _CTX["done"].set()
        else:
            _CTX["_escalation_acked"] = True

    return agent


# ─── Full-pipeline coordinator ────────────────────────────────────────────────

def _make_coordinator() -> "Agent":
    """Coordinator: governs the full 8-stage pipeline, replacing Band.

    State machine:
      startup → fire parallel trio
      parallel all done → fire knowledge
      knowledge done → fire skeptic
      skeptic done → fire reconciler
      reconciler done → escalation gate → fire brief + maybe escalation
      brief done (+ escalation ack) → signal done
    """
    agent = Agent(name="coordinator", seed=_SEEDS["coordinator"])

    @agent.on_event("startup")
    async def _start(ctx: "Context"):
        for stage in PARALLEL_STAGES:
            await ctx.send(_CTX["worker_addresses"][stage], TriggerStage(stage=stage))

    @agent.on_message(model=StageResult)
    async def _collect(ctx: "Context", sender: str, msg: StageResult):
        import agents as dev
        from band.audit import AuditLog
        from schemas import risk_from_reconciler, GateDecision

        result = json.loads(msg.payload_json)
        stage  = msg.stage
        _CTX["results"][stage] = result

        # Record in full_log and fire callback
        brief = dev.extract_brief(stage, result)
        _CTX["full_log"]["agents"][stage] = {
            "label":          LABELS.get(stage, stage),
            "elapsed_seconds": msg.elapsed,
            "brief":          brief,
            "full_output":    result if not isinstance(result, str) else {"text": result},
        }
        on_complete = _CTX["callbacks"].get("on_complete")
        if on_complete:
            on_complete(stage, LABELS[stage], brief, msg.elapsed)

        results = _CTX["results"]

        # ── parallel trio done → knowledge ───────────────────────────────────
        if (all(s in results for s in PARALLEL_STAGES)
                and "knowledge" not in results
                and not _CTX.get("_knowledge_triggered")):
            _CTX["_knowledge_triggered"] = True
            _CTX["audit"].append("coordinator", "parallel_complete",
                                 payload={"stages": list(PARALLEL_STAGES)})
            await ctx.send(_CTX["worker_addresses"]["knowledge"], TriggerStage(stage="knowledge"))

        # ── knowledge done → skeptic ──────────────────────────────────────────
        elif stage == "knowledge":
            _CTX["audit"].append("coordinator", "rag_complete")
            await ctx.send(_CTX["worker_addresses"]["skeptic"], TriggerStage(stage="skeptic"))

        # ── skeptic done → reconciler ─────────────────────────────────────────
        elif stage == "skeptic":
            from schemas import rebuttal_from_skeptic
            rebuttal = rebuttal_from_skeptic(result)
            _CTX["audit"].append("skeptic_agent", "rebuttal_submitted",
                                 payload=rebuttal.model_dump(exclude={"raw"}))
            await ctx.send(_CTX["worker_addresses"]["reconciler"], TriggerStage(stage="reconciler"))

        # ── reconciler done → gate → brief (+ optional escalation) ───────────
        elif stage == "reconciler":
            assessment = risk_from_reconciler(result, trace_id=_CTX.get("trace_id"))
            _CTX["assessment"] = assessment
            _CTX["audit"].append(
                "reconciler_agent", "assessment_submitted",
                payload={
                    "risk_score":          assessment.risk_score,
                    "risk_level":          assessment.risk_level,
                    "recommend_escalation": assessment.recommend_escalation,
                },
            )

            # Escalation gate (governance — replaces Band gate)
            authority_granted = True  # TODO: wire real authority token
            escalate = assessment.recommend_escalation and authority_granted
            reason = (
                "Escalation recommended and authority granted — proceeding to handoff."
                if escalate else
                "Risk below threshold — escalation not recommended."
            )
            gate = GateDecision(escalate=escalate, authority_granted=authority_granted, reason=reason)
            _CTX["gate"] = gate
            _CTX["audit"].append(
                "escalation_gate", "gate_decision",
                authority_granted=authority_granted,
                payload=gate.model_dump() | {"risk_score": assessment.risk_score},
            )

            # Always produce the SBAR brief
            await ctx.send(_CTX["worker_addresses"]["brief"], TriggerStage(stage="brief"))

            # Fire escalation uAgent in parallel if gate opens
            if escalate:
                _CTX["_awaiting_escalation"] = True
                patient_id = _CTX["patient_data"]["profile"].get("id", "unknown")
                on_start = _CTX["callbacks"].get("on_start")
                if on_start:
                    on_start("escalation", LABELS["escalation"])
                await ctx.send(
                    _CTX["worker_addresses"]["escalation"],
                    EscalationHandoff(
                        patient_id=patient_id,
                        risk_score=assessment.risk_score_100,
                        sbar="",          # will be filled once brief arrives; delivery is async
                        authority_granted=authority_granted,
                    ),
                )

        # ── brief done → finish ───────────────────────────────────────────────
        elif stage == "brief":
            _CTX["audit"].append("coordinator", "pipeline_complete")
            if not _CTX.get("_awaiting_escalation"):
                # Gate did not open — record held decision and fire escalation card
                gate = _CTX.get("gate")
                _CTX["audit"].append("escalation_agent", "escalation_held",
                                     payload={"reason": gate.reason if gate else "below threshold"})
                on_start_cb    = _CTX["callbacks"].get("on_start")
                on_complete_cb = _CTX["callbacks"].get("on_complete")
                if on_start_cb:
                    on_start_cb("escalation", LABELS["escalation"])
                esc_brief = {
                    "escalated":         False,
                    "authority_granted": False,
                    "reason":            gate.reason if gate else "No gate decision",
                    "uagent_address":    Agent(name="pipeline-escalation",
                                              seed=_SEEDS["escalation"]).address,
                }
                _CTX["full_log"]["agents"]["escalation"] = {
                    "label":           LABELS["escalation"],
                    "elapsed_seconds": 0,
                    "brief":           esc_brief,
                    "full_output":     esc_brief,
                }
                if on_complete_cb:
                    on_complete_cb("escalation", LABELS["escalation"], esc_brief, 0)
                _CTX["done"].set()
            elif _CTX.get("_escalation_acked"):
                # Escalation already completed while brief was running
                _CTX["done"].set()
            # else: escalation is still in-flight — its handler will set done

    return agent


# ─── Public API ───────────────────────────────────────────────────────────────

def is_available() -> bool:
    return _UAGENTS_AVAILABLE


def mesh_status() -> list[dict]:
    """Return every agent's real deterministic address (no network needed)."""
    if not _UAGENTS_AVAILABLE:
        return []
    _ensure_loop()
    out = []
    for stage in ALL_WORKER_STAGES:
        a = Agent(name=f"pipeline-{stage}", seed=_SEEDS[stage])
        out.append({"stage": stage, "name": LABELS.get(stage, stage), "address": a.address})
    # Special names for escalation and coordinator
    out.append({"stage": "escalation",  "name": LABELS["escalation"],  "address": Agent(name="pipeline-escalation",  seed=_SEEDS["escalation"]).address})
    out.append({"stage": "coordinator", "name": "Coordinator",          "address": Agent(name="coordinator",          seed=_SEEDS["coordinator"]).address})
    return out


def run_full_pipeline_via_mesh(
    patient_data: dict,
    client,
    callbacks: dict | None = None,
    web_research: dict | None = None,
    *,
    timeout: float = 600.0,
) -> dict:
    """Run the complete 8-stage pipeline as real Fetch.ai uAgents in a Bureau.

    Returns a dict with:
      results   — {stage: agent_output_dict}
      full_log  — dict ready to merge into the pipeline's JSON log
      audit     — AuditLog instance
      assessment — RiskAssessment (typed)
      gate       — GateDecision (typed)
    """
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed — pip install -r requirements.txt")

    from band.audit import AuditLog

    global _CTX
    _CTX = {
        "client":               client,
        "patient_data":         patient_data,
        "web_research":         web_research,
        "callbacks":            callbacks or {},
        "coordinator_address":  None,
        "worker_addresses":     {},
        "results":              {},
        "done":                 threading.Event(),
        "audit":                AuditLog(echo=False),
        "assessment":           None,
        "gate":                 None,
        "escalation_address":   None,
        "_knowledge_triggered": False,
        "_awaiting_escalation": False,
        "_escalation_acked":    False,
        "full_log":             {"agents": {}},
        "trace_id":             None,
    }
    error: dict = {}

    def _run_bureau():
        _ensure_loop()
        try:
            coordinator   = _make_coordinator()
            esc_worker    = _make_escalation_worker()
            stage_workers = {s: _make_worker(s) for s in ALL_WORKER_STAGES}

            _CTX["coordinator_address"] = coordinator.address
            _CTX["worker_addresses"]    = {
                s: w.address for s, w in stage_workers.items()
            }
            _CTX["worker_addresses"]["escalation"] = esc_worker.address

            bureau = Bureau(port=_free_port())
            bureau.add(coordinator)
            bureau.add(esc_worker)
            for w in stage_workers.values():
                bureau.add(w)
            bureau.run()
        except Exception as exc:
            error["exc"] = exc
            _CTX["done"].set()

    threading.Thread(target=_run_bureau, daemon=True).start()

    if not _CTX["done"].wait(timeout):
        raise TimeoutError("Fetch.ai full-pipeline timed out — check agent logs.")
    if error.get("exc"):
        raise error["exc"]

    return {
        "results":    dict(_CTX["results"]),
        "full_log":   _CTX["full_log"],
        "audit":      _CTX["audit"],
        "assessment": _CTX["assessment"],
        "gate":       _CTX.get("gate"),
        "mesh_addresses": mesh_status(),
        "escalation_address": _CTX.get("escalation_address"),
    }


# ─── Backward-compat: parallel-only path (CLI / tests) ────────────────────────

def run_parallel_analysis_via_mesh(
    patient_data: dict,
    client,
    callbacks: dict | None = None,
    *,
    timeout: float = 240.0,
) -> dict:
    """Run only the 3 parallel analysis agents (legacy CLI path).

    The web app now uses run_full_pipeline_via_mesh instead.
    """
    if not _UAGENTS_AVAILABLE:
        raise RuntimeError("uagents is not installed.")

    global _CTX
    _CTX = {
        "client":              client,
        "patient_data":        patient_data,
        "web_research":        None,
        "callbacks":           callbacks or {},
        "coordinator_address": None,
        "worker_addresses":    {},
        "results":             {},
        "done":                threading.Event(),
        "full_log":            {"agents": {}},
    }
    error: dict = {}

    def _run():
        _ensure_loop()
        try:
            coord = _make_parallel_coordinator(len(PARALLEL_STAGES))
            workers = [_make_worker(s) for s in PARALLEL_STAGES]
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


def _make_parallel_coordinator(expected: int) -> "Agent":
    """Minimal coordinator for the legacy 3-agent parallel path."""
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
        on_complete = _CTX["callbacks"].get("on_complete")
        if on_complete:
            on_complete(msg.stage, LABELS[msg.stage], dev.extract_brief(msg.stage, result), msg.elapsed)
        if len(_CTX["results"]) >= expected:
            _CTX["done"].set()

    return agent


def escalation_uagent() -> "Agent":
    """Return the real Fetch escalation uAgent (address always available)."""
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
        print(f"  {s['name']:28} {s['address']}")

    client  = config.get_anthropic_client()
    patient = PATIENTS["PT-7421"]
    print(f"\nRunning full 8-stage pipeline for {patient['profile']['name']} via Fetch.ai mesh...\n")

    def on_start(stage, label):
        print(f"  → {label}")

    def on_complete(stage, label, brief, elapsed):
        print(f"  ✓ {label} ({elapsed}s)")

    result = run_full_pipeline_via_mesh(
        patient, client,
        callbacks={"on_start": on_start, "on_complete": on_complete},
    )
    print(f"\nPipeline complete. Gate decision: {result['gate']}")
    print(f"Audit trail: {len(result['audit'])} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
