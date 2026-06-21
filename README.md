# Nightingale

AI-powered post-discharge patient monitoring. An 8-agent pipeline detects early clinical deterioration across live vitals, sensor trends, and patient language — and escalates with a structured clinical handoff before it becomes an emergency.

**Backend:** FastAPI + Python &nbsp;|&nbsp; **Frontend:** Vanilla JS, single HTML file &nbsp;|&nbsp; **LLM:** Claude Opus 4

---

## The problem

Patients discharged from hospital are at highest readmission risk in the first 7 days. Point-in-time vitals miss the patients who slide *slowly* — resting HR creeping up, sleep fragmenting, activity falling, self-reports quietly minimizing. Five signals each drifting a little can't all be coincidence at once.

Nightingale surfaces convergent deterioration, then makes a skeptic agent argue against escalation to fight alert fatigue, before a reconciler judge weighs both sides and makes a governed decision.

---

## 8-Agent Pipeline

```
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│ Signal Agent │  │  Trend Agent  │  │ Self-Report NLP  │   ← parallel (Fetch.ai uAgents)
└──────┬───────┘  └──────┬───────┘  └────────┬─────────┘
       └─────────────────┴───────────────────┘
                         │ all findings
                         ▼
              ┌──────────────────────┐
              │  Medical RAG Agent   │   ← PubMed/NIH knowledge retrieval
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  Adversarial Skeptic │   ← hypothesis → rebuttal debate rounds
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  Reconciler / Judge  │   ← rules on each debate round, risk 0–100
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  Clinical Brief      │   ← SBAR handoff document
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  Escalation Agent    │   ← Fetch.ai fire-and-forget handoff
              └──────────────────────┘
```

---

## Sponsor Integrations

### Fetch.ai uAgents
Every pipeline stage is a real `uagents.Agent` running in a local `Bureau`. The 3 parallel analysis agents receive a `TriggerStage` message and send their `StageResult` back to the Coordinator. The sequential chain (RAG → Skeptic → Reconciler → Brief) runs inline on the Coordinator via `asyncio.to_thread` — no extra message round-trips. Escalation is fire-and-forget: the pipeline signals done the moment Brief completes.

All agent addresses are deterministic ed25519 keypairs derived from seeds (same format as Agentverse cloud agents). No network required — adding `endpoint=` and Almanac registration would make them discoverable globally with no other code changes.

### Arize / Phoenix
OpenTelemetry tracing wraps the full pipeline span and each agent step. The Anthropic SDK is auto-instrumented so every Claude call appears as a span. A self-correction loop nudges `RISK_THRESHOLD` ±0.05 on false positives/negatives when a patient has a ground-truth label. No-op unless `ENABLE_TRACING` or `ARIZE_API_KEY` is set.

### Band
Governance fallback when running without the Fetch mesh. `BandRoom.deliberate()` runs one bounded Skeptic→Reconciler round. `escalation_gate()` performs a human-in-the-loop authority check. Append-only `AuditLog` records every consequential decision timestamped and serialized to JSON.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Python 3.11+ |
| LLM | Claude Opus 4 (`claude-opus-4-8`) |
| Agent mesh | Fetch.ai `uagents` |
| Observability | Arize Phoenix / OpenTelemetry |
| Governance | Band Protocol |
| Frontend | Vanilla JS + HTML/CSS (no build step) |
| Streaming | Server-Sent Events (SSE) |
| Voice | Deepgram STT + TTS |
| Web research | Browserbase + PubMed/NIH scraping |
| Hardware | Arduino (optional — tilt + light sensors) |

---

## Setup

```bash
git clone https://github.com/Jason2657/recovery-monitor.git
cd recovery-monitor
pip install -r requirements.txt
cp .env.example .env   # add your keys
python app.py          # → http://localhost:8000
```

### Environment variables

```bash
ANTHROPIC_API_KEY=...          # required

# Optional
DEEPGRAM_API_KEY=...           # voice input / TTS
BROWSERBASE_API_KEY=...        # web research via Browserbase
BROWSERBASE_PROJECT_ID=...
ARIZE_API_KEY=...              # Arize cloud tracing
ENABLE_TRACING=true            # local Phoenix tracing (run `phoenix serve` first)
ANTHROPIC_MODEL=...            # override model (default: claude-opus-4-8)
```

---

## Patients

Six built-in synthetic patients covering distinct clinical scenarios:

| Patient | Condition | Expected outcome |
|---|---|---|
| James Carter (PT-7421) | CHF decompensation | **Escalate** — fluid overload, NEWS2 rising |
| Eleanor Park (PT-3892) | Post-TKA surgical site infection | **Escalate** — fever 38.9°C, spreading infection |
| Robert Chen (PT-5163) | COPD exacerbation recurrence | **Escalate** — SpO2 87%, RR 27, rescue inhaler ×5/day |
| Maria Gonzalez (PT-8427) | Post-pneumonia deterioration | **Escalate** — fever + declining sats |
| Grace Liu (PT-2048) | Post-lap-chole (elective) | **No escalation** — uneventful recovery, Skeptic wins |
| Charles Gordon (PT-1847) | Post-appendectomy | **No escalation** — textbook recovery, all vitals normal |

Custom patients can be created through the UI with a 7-day vitals wizard (supports voice entry via Deepgram).

---

## Terminal demo

```bash
python run_demo.py --patient PT-7421          # CHF (escalates)
python run_demo.py --patient PT-1847          # Charles Gordon (no escalation)
python run_demo.py --patient PT-7421 --mesh   # run via Fetch.ai Bureau
python run_demo.py --list                     # show all patients

python -m mesh.fetch_mesh                     # print all uAgent addresses
```

---

## Project structure

```
app.py                  FastAPI backend, SSE streaming, Arduino reader
agents.py               All 8 Claude agent functions + pipeline orchestrator
patient_data.py         Synthetic patient scenarios
schemas.py              Pydantic models: RiskAssessment, GateDecision, AuditRecord
config.py               Model selection, RISK_THRESHOLD knob, API keys
mesh/
  fetch_mesh.py         Fetch.ai Bureau + Coordinator + all uAgents
band/
  room.py               BandRoom governance (deliberation + escalation gate)
  audit.py              Append-only AuditLog
observability/
  tracing.py            Arize/Phoenix OpenTelemetry wrapper
  evaluator.py          Self-correction loop
web_research.py         PubMed/NIH + Browserbase browser automation
static/index.html       Entire frontend (single file, no build step)
tests/                  Unit tests (governance, evaluator, tracing, mesh)
```

---

## How the debate works

The **Skeptic** generates structured debate rounds — each with a benign hypothesis explaining why the patient is probably fine. The **Reconciler** receives the full transcript and rules on each round explicitly (`accepted` / `overruled`) with reasoning. The UI renders the full back-and-forth with color-coded rulings and a final verdict.

Risk scores below `RISK_THRESHOLD` (default 0.5) suppress escalation. The Arize self-correction loop adjusts this threshold automatically based on ground-truth outcomes.

---

## Hardware (optional)

An Arduino with a tilt sensor and photoresistor plugs in over serial. Tilt events track sleep movement and disturbance counts. The photoresistor detects light-on wake events. Both feed into the live vitals stream at `/api/live/{patient_id}` and appear in the Signal agent's analysis.
