# Recovery Monitor

A post-surgery recovery **trend-and-triage early-warning tool** for patients
recovering at home. A cheap hardware sensor (heart-rate, tilt/mobility,
ambient-light, touch check-in, LCD) streams data; a layer of specialized agents
watches for *slow* deterioration, argues with itself before bothering a
clinician, and escalates with a proper clinical handoff. It is **decision-support
that loops in humans — not a diagnostic device.** Our initial target is
colorectal surgery recovery, where low patient mobility is a validated predictor
of readmission, so the trend in day-to-day mobility (not any single reading) is
the signal we care about most.

> ⚠️ Not a medical device. Every consequential action is gated on a human
> authority check and produces an audit trail. The system surfaces concerns and
> drafts a handoff; a clinician decides.

## Why this shape

Deterioration at home is usually slow. A single reading looks fine; the danger is
the trajectory over days. And a naive alerting system cries wolf — so we build in
an explicit **Skeptic** that argues *against* escalation before a **Reconciler**
weighs both sides. Humans stay in the loop at the gate.

## Architecture

```
  inputs ──→ parallel analysis ──→ Band room (governed) ──→ outputs

  ┌─────────────────────┐
  │ live sensor stream   │     ┌──────────────────────────────┐
  │ daily self-report    │ ──▶ │ Signal agent   (acute)        │
  │ (text / voice note)  │     │ Trend agent    (5–7 day drift)│  run
  └─────────────────────┘     │ Self-report NLP (vague→signal) │  concurrently
                               └──────────────┬───────────────┘
                                              │ findings
                          early-warning score │ (deterministic tool)
                                              ▼
                          ┌───────────────────────────────────┐
                          │ BAND ROOM  (governed coordination) │
                          │   Skeptic  ⇄  Reconciler/Judge      │
                          │        ↓ calibrated risk + trace    │
                          │   escalation gate (authority+audit) │
                          └───────────────┬───────────────────┘
                            risk high      │       benign / Skeptic wins
                  ┌────────────────────────┘────────────────────┐
                  ▼                                              ▼
        Clinician brief (SBAR)                         patient LCD status only
                  │
                  ▼
        Escalation uAgent (Fetch) ─→ care coordinator

        ── the patient LCD is ALWAYS updated on both paths ──
        ── the whole pipeline runs inside the Arize trace boundary ──
```

See [docs/architecture.md](docs/architecture.md) for the prose walkthrough.

## Sponsor boundaries (each is load-bearing)

| Sponsor | Role here | Where in the code |
|---|---|---|
| **Fetch.ai uAgents** | *Builds the agents.* Each specialized worker is a Fetch uAgent (message-passing). | `agents/` — `BaseAgent.as_uagent()` is the mesh bridge; `escalation_agent.py` is the outbound notifier. |
| **Band** | *Governs the decision.* The room where agents reconcile under verified authority and emit an audit trail. Behind an adapter so the backend is swappable. | `band/room.py` (logic), `band/adapter.py` (`LocalBandRoom` default + `RemoteBandRoom` stub), `band/audit.py`. |
| **Arize / Phoenix** | *Observes + corrects.* Tracing over every agent step, plus the self-correction loop: evals on the Reconciler's risk calls feed back into one tunable threshold. | `observability/tracing.py`, `observability/evaluator.py`. |

The boundaries are clean on purpose: Fetch is the *who* (agents), Band is the
*how decisions are made and authorized* (governance), Arize is the *did it work,
and how do we tune it* (observability + feedback).

## Setup

Requires Python 3.11+.

```bash
# with uv (preferred)
uv sync

# or with venv + pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# configure (optional — the demo runs without any keys)
cp .env.example .env   # then fill in keys when you wire real logic
```

The demo runs **without any API keys**: agent logic is stubbed and tracing
no-ops gracefully when `ARIZE_*` keys are absent. Keys are only needed once you
swap stubs for real Claude calls / remote observability / the Band API.

## Run

```bash
python -m recovery_monitor.run_demo --profile deterioration
python -m recovery_monitor.run_demo --profile benign_decoy
# options: --speed 0.4 (watchable pace), --days 7, --quiet
```

- **`deterioration`** — slow real decline; the gate escalates by the end of the
  week and you'll see the SBAR handoff + escalation notice.
- **`benign_decoy`** — looks alarming mid-week, but the Skeptic catches the
  benign explanation (travel/visitors) and the gate holds. The patient LCD still
  updates.

Run the tests (this is the day-one integration guarantee):

```bash
pytest            # test_pipeline_smoke.py runs the full pipeline end-to-end
```

## Project status

This is a **runnable skeleton**. The pipeline is wired end-to-end and proven by
`tests/test_pipeline_smoke.py`; the agent *logic* is stubbed (look for
`TODO(owner)` markers) so the team can fill in real implementations behind stable
interfaces without breaking integration. The early-warning score tool
(`tools/early_warning.py`) is the one piece that is fully implemented.

**Who owns what:** see [docs/division-of-labor.md](docs/division-of-labor.md).
