# Architecture

Recovery Monitor is a decision-support pipeline (not a diagnostic device) for
home post-surgical recovery. It turns a noisy sensor stream + daily self-reports
into either a calm patient status or a properly-governed clinical escalation.

## The flow

```
  inputs ──→ parallel analysis ──→ Band room (governed) ──→ outputs
```

### 1. Inputs

- **Live sensor stream** from the hardware kit: heart-rate, tilt/mobility,
  ambient-light, touch check-in, and an LCD for output. In the demo this is
  replayed from a synthetic timeline (`sim/sensors.py` + `data/synthetic.py`).
- **Daily patient self-report** — a short free-text or transcribed voice note.

These are assembled, each timestep, into a `PatientContext` (`schemas.py`): the
latest reading, a rolling 5–7 day window, and the self-reports so far. That single
object is what every analysis agent receives.

### 2. Parallel analysis (run concurrently)

Three specialized agents run together (`asyncio.gather`), each looking at the
context through a different lens:

- **Signal agent** (`agents/signal_agent.py`) — *acute* anomalies in the latest
  reading. The "something is wrong right now" view.
- **Trend agent** (`agents/trend_agent.py`) — *the intellectual core.* Rolling
  5–7 day drift: resting HR creeping (~+2 bpm/day), rising night-time light-on
  frequency (sleep fragmenting), mobility trending down. Slow decline is what
  point-in-time checks miss, and low mobility is the validated colorectal
  readmission predictor.
- **Self-report NLP agent** (`agents/selfreport_nlp_agent.py`) — turns vague
  language ("a bit more tired", "didn't sleep great") into a structured symptom
  signal.

Each returns an `AgentFinding` (summary, severity 0–1, evidence).

### 3. Deterministic early-warning score

`tools/early_warning.py` computes a **simplified, NEWS2-inspired** score from the
vitals the kit actually captures (heart-rate, mobility, an ambient-light/sleep
proxy, and missed touch check-ins). It is deterministic and fully implemented —
not an LLM, and **not** clinical NEWS2 (which needs respiratory rate, SpO2,
temperature, BP, and consciousness this kit can't measure). It is one more signal
the Reconciler weighs.

### 4. Band room (governed coordination)

This is where the decision is *made and authorized* (`band/`).

- **Skeptic agent** (`agents/skeptic_agent.py`) argues *against* escalation,
  hunting for benign explanations. Institutional doubt that stops false alarms.
- **Reconciler / Judge** (`agents/reconciler_agent.py`) weighs the findings + the
  early-warning score against the Skeptic's rebuttal and produces a calibrated
  `RiskAssessment`: a risk score, a recommend/hold call, and a bullet-point
  reasoning trace. `recommend_escalation` is gated on the single tunable
  `RISK_THRESHOLD`.
- **`BandRoom.deliberate`** runs exactly **one bounded round** (Skeptic →
  Reconciler) — no infinite loop.
- **Escalation gate** (`BandRoom.escalation_gate`) enforces the
  **human-in-the-loop authority check**: the system may only act when the
  Reconciler recommends escalation *and* a verified authority grants it. Every
  consequential step writes an `AuditRecord` (`band/audit.py`).

The backend lives behind an adapter (`band/adapter.py`): `LocalBandRoom` runs
in-process (default); `RemoteBandRoom` is the stub for the real Band API.

### 5. Outputs — the gate forks two ways

- **Risk high** → **Clinician brief agent** (`agents/clinician_brief_agent.py`)
  writes an **SBAR** handoff (Situation, Background, Assessment, Recommendation),
  and the **Escalation uAgent** (`agents/escalation_agent.py`, Fetch.ai) notifies
  a care coordinator.
- **Benign / Skeptic wins** → patient **LCD** status only.

**The patient LCD is always updated on both paths** (`sim/lcd.py`) — the patient
always gets a clear, calm status.

### Cross-cutting: the Arize trace boundary

The whole pipeline runs inside an Arize/Phoenix trace (`observability/tracing.py`).
Every `evaluate()` and the Band `deliberate()`/gate are wrapped in spans. Tracing
no-ops gracefully when no keys are present, so the repo runs without an account.

The **self-correction loop** (`observability/evaluator.py`) closes the circle:
evals score the Reconciler's risk calls against ground-truth outcomes and nudge
the single `RISK_THRESHOLD` knob — too many false alarms raise it, missed
deterioration lowers it.

## Why these boundaries

| Concern | Owner | Rationale |
|---|---|---|
| The agents | **Fetch.ai uAgents** | Specialized, message-passing workers. |
| The decision + authority + audit | **Band** | Governed reconciliation under verified authority. |
| Observability + feedback | **Arize / Phoenix** | See every step; tune one knob from evals. |

Keeping these separable means any one can be swapped (e.g. `LocalBandRoom` →
`RemoteBandRoom`) without disturbing the others.

## Data contract

All shared types live in `schemas.py` (pydantic v2). Agreeing on these first is
what lets four people build in parallel:

```
SensorReading · SelfReport · PatientContext       (inputs)
AgentFinding · EarlyWarningScore                   (analysis)
SkepticRebuttal · RiskAssessment                   (deliberation)
GateDecision · SBARBrief · AuditRecord             (gate + outputs)
```
