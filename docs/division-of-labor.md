# Division of labor

A suggested 4-way split for a hackathon team. Each person owns a **coherent
vertical** so you can work in parallel against the shared contract in
`schemas.py`. This is *easily re-sliced* — swap pieces to match interests, just
keep the schema boundaries stable.

> First rule: **`schemas.py` is the contract.** If you need to change a shared
> model, say so loudly — it affects everyone. Code against the types, not against
> each other's internals.

## Shared / first-in (do this together, up front)

Already scaffolded; whoever touches each first effectively owns it:

- **`schemas.py`** — the pydantic contract. Lock the field names early.
- **`config.py`** — env loading, the Anthropic client factory, the single
  `RISK_THRESHOLD` knob.
- **`observability/tracing.py`** — the Arize/Phoenix trace boundary. Whoever
  picks up Arize first also owns the **correction loop** in
  `observability/evaluator.py`.

The end-to-end pipeline (`run_demo.py`) already runs on stubs — keep it green
(`pytest`) as you replace stubs with real logic.

---

## Person A — Drift & data *(the intellectual core)*

**Owns:** `agents/trend_agent.py` + `data/synthetic.py`

These ship together: the Trend agent is only as good as the timelines it's tested
against, so build them as a pair.

- Implement real rolling-window drift detection: per-signal slopes (resting HR
  creep, mobility decline, rising night-time light-on), fused into a severity.
- Evolve the two synthetic profiles so the agent is genuinely tested: the
  `deterioration` curve should be catchable, the `benign_decoy` should be
  *tempting but wrong*.
- **Definition of done:** Trend agent's severity clearly separates the two
  profiles across the window; `tests/test_pipeline_smoke.py` still green.

## Person B — Debate & governance

**Owns:** `agents/skeptic_agent.py` + `agents/reconciler_agent.py` +
`band/room.py`

The reconcile-then-gate heart of the system.

- Make the **Skeptic** genuinely argue (Claude call): read findings + EWS +
  self-reports, produce real benign counterpoints + a calibrated
  `suppression_strength`.
- Make the **Reconciler** a real judge (Claude call): weigh evidence vs.
  rebuttal into a calibrated risk + reasoning trace; keep the `RISK_THRESHOLD`
  gating so the correction loop still owns the boundary.
- Harden the **escalation gate**: the human-in-the-loop authority check + audit.
  Keep deliberation **bounded** (one round).
- **Definition of done:** benign decoy is suppressed for the right reason;
  rationale traces are legible in the audit log.

## Person C — Sensing & scoring

**Owns:** `agents/signal_agent.py` + `sim/` (sensors + LCD) +
`tools/early_warning.py`

The hardware-facing + deterministic slice.

- Implement the **Signal agent** (acute, single-reading anomalies vs. baseline).
- `tools/early_warning.py` is already fully implemented — own it, refine the
  thresholds, keep its tests strong (`tests/test_early_warning.py`).
- Build out `sim/sensors.py` toward a real device stream and `sim/lcd.py` toward a
  real LCD backend (keep the same interfaces).
- **Definition of done:** a believable live stream + a clear patient LCD;
  early-warning thresholds defensible.

## Person D — Language & handoff

**Owns:** `agents/selfreport_nlp_agent.py` + `agents/clinician_brief_agent.py` +
`agents/escalation_agent.py`

The unstructured-input and output-action slice.

- **Self-report NLP** (Claude call): vague note → structured symptoms + severity.
- **Clinician brief** (Claude call): a fluent, grounded **SBAR** handoff.
- **Escalation uAgent** (Fetch.ai): turn `notify()` into a real mesh send to a
  care-coordinator agent (wire `BaseAgent.as_uagent()` / a bureau).
- **Definition of done:** a real SBAR reaches a (simulated) coordinator over the
  Fetch mesh; the send is audited.

---

## Integration checkpoints

1. **Now:** skeleton runs end-to-end on stubs (`pytest` green). ✅
2. **Mid:** each owner's real logic lands behind its interface; smoke test stays
   green the whole way.
3. **End:** turn on tracing with real `ARIZE_*` keys, demo both profiles live,
   show the correction loop nudging `RISK_THRESHOLD` from evals.

If integration breaks, fix the **interface mismatch** — don't fork the schema.
