# PostCare AI Monitor

A post-discharge **recovery trend-and-triage early-warning tool** for patients
recovering at home. A layer of specialized agents watches for slow deterioration,
**argues with itself before bothering a clinician**, and escalates with a proper
clinical handoff (SBAR) under human authority.

This is **decision-support that loops in a human — not a diagnostic device.** It
was originally anchored on colorectal-surgery recovery (where low patient mobility
is a validated readmission predictor) and now spans several post-discharge
conditions (CHF, surgical-site infection, COPD, pneumonia) plus a benign-decoy
case the system is supposed to *not* over-escalate.

## Why it's interesting

Point-in-time vitals miss the patients who slide slowly. Five signals each drifting
a little — resting HR creeping, sleep fragmenting, activity falling, weight rising,
self-reports quietly minimizing — can't all be coincidence at once. The system
surfaces *convergent* deterioration, then makes a **skeptic argue against
escalation** to fight alert fatigue before a **reconciler** weighs both sides.

## Architecture

```
  sensors + daily self-report
        │
        ▼
 ┌───────────────────── Arize / Phoenix trace boundary ──────────────────────┐
 │                                                                            │
 │   parallel analysis  ──  Fetch.ai uAgents (independent workers)            │
 │     Signal · Trend · Self-report NLP                                       │
 │            │                                                               │
 │            ▼                                                               │
 │     Medical Knowledge (RAG over a clinical guideline base)                 │
 │            │                                                               │
 │            ▼   Band room  ──  governed coordination                        │
 │     Skeptic  ⇄  Reconciler/Judge                                           │
 │            │                                                               │
 │            ▼   escalation gate  (verified authority + append-only audit)   │
 │            ├─ escalate → SBAR brief → Escalation uAgent (Fetch) → coord.   │
 │            └─ hold     → patient status only                               │
 │                                                                            │
 └────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  self-correction: evals on the Reconciler's calls nudge ONE RISK_THRESHOLD knob
```

### Sponsor boundaries (each is load-bearing, kept swappable)

| Concern | Sponsor | Where it lives |
|---|---|---|
| **The agents** — specialized message-passing workers | **Fetch.ai uAgents** | [`mesh/fetch_mesh.py`](mesh/fetch_mesh.py) — the 3 analysis agents run as real `uagents.Agent`s in a `Bureau`; the Escalation uAgent is the output-path worker |
| **The decision** — reconcile under verified authority + audit | **Band** | [`band/`](band/) — `BandRoom.deliberate` (one bounded Skeptic⇄Reconciler round) + `escalation_gate` (human-in-the-loop authority check) + append-only audit, behind a `LocalBandRoom`/`RemoteBandRoom` adapter |
| **Observability + feedback** | **Arize / Phoenix** | [`observability/`](observability/) — every step is a span, Claude calls auto-instrument, and the self-correction loop tunes one threshold |

Keeping these behind clean seams means any one can be swapped (e.g.
`LocalBandRoom` → a real Band service) without disturbing the others.

> **Note on Band:** the governance layer is fully implemented in-process today.
> [`band/adapter.py`](band/adapter.py) `RemoteBandRoom` is the seam where a real
> Band API/SDK plugs in — confirm with the organizers whether "Band" maps to a
> specific product and drop its client there.

## The agents

| # | Agent | Role |
|---|---|---|
| 1 | Signal | Acute anomalies in today's vitals (pre-computed NEWS2 fed in) |
| 2 | Trend | 7-day drift — the intellectual core; convergent slow decline |
| 3 | Self-report NLP | Vague patient language → structured symptom signal |
| 4 | Medical Knowledge (RAG) | Applies a clinical guideline base (NEWS2, CHF, SSI, COPD, SIRS, activity-decline) |
| 5 | Skeptic | Argues **against** escalation; hunts benign explanations |
| 6 | Reconciler / Judge | Weighs evidence vs. the skeptic → calibrated 0–100 risk |
| 7 | Clinical Brief | SBAR handoff for a care coordinator |
| + | Escalation (Fetch uAgent) | Governed output action — fires only on an authorized escalate |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install -r requirements.txt                       # or: uv pip install -r requirements.txt
cp .env.example .env                                  # then add ANTHROPIC_API_KEY
```

Standardized on Claude (`claude-opus-4-8` by default; override with
`ANTHROPIC_MODEL`). The client and the single `RISK_THRESHOLD` knob are centralized
in [`config.py`](config.py).

## Run

**Terminal demo (shows all three sponsor tracks):**

```bash
python run_demo.py --patient PT-7421          # CHF decompensation (should escalate)
python run_demo.py --patient PT-2048          # benign decoy (Skeptic should win)
python run_demo.py --patient PT-7421 --mesh   # parallel analysis via the Fetch.ai uAgents mesh
python run_demo.py --list
```

It prints the 7-agent reasoning, then the **Band escalation gate + audit trail**,
the **Arize self-correction** result, and the SBAR brief.

**Fetch.ai mesh on its own** (boots the real Bureau, real uAgent addresses):

```bash
python -m mesh.fetch_mesh
```

**Web app** (FastAPI + SSE streaming + Deepgram voice):

```bash
python app.py        # → http://localhost:8000
```

**Observability (optional).** Tracing is a clean no-op until you turn it on:

```bash
pip install arize-phoenix && phoenix serve    # local UI at http://localhost:6006
ENABLE_TRACING=1 python run_demo.py --patient PT-7421
```

…or point at Arize cloud with `ARIZE_API_KEY` + `ARIZE_SPACE_ID`.

## The self-correction loop

The only knob the feedback loop moves is `RISK_THRESHOLD`. Each patient carries a
`ground_truth_escalate` label; after a run the evaluator grades the Reconciler's
call and nudges the knob — a false alarm **raises** it (be calmer), a missed
deterioration **lowers** it (be keener). Run the benign decoy (`PT-2048`) to watch
it raise the threshold after an over-escalation.

## Tests

```bash
pip install pytest && pytest -q
```

Covers the governance gate + audit, the bounded deliberation round, the
self-correction knob, the tracing no-op fallback, and the Fetch uAgent addresses
(16 tests, no API key required).

## Repo layout

```
config.py            model + Anthropic client + the RISK_THRESHOLD knob
schemas.py           pydantic governance contract (RiskAssessment, GateDecision, AuditRecord, …)
agents.py            the 7 agent bodies + the governed run_full_pipeline orchestrator
patient_data.py      synthetic patients + clinical knowledge base
band/                Band governance: audit · room (deliberate + gate) · adapter
observability/       Arize/Phoenix: tracing · evaluator (self-correction)
mesh/                Fetch.ai uAgents Bureau (real message-passing workers)
run_demo.py          sponsor-aware terminal demo
main.py              brief terminal runner
app.py               FastAPI web backend (SSE + Deepgram)
static/index.html    web UI
tests/               governance / evaluator / tracing / mesh / import tests
```
