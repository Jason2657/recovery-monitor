"""LLM-as-a-judge evaluator — the Arize "evaluator".

A real LLM-prompt judge of OUTPUT QUALITY, distinct from the deterministic
RISK_THRESHOLD self-correction loop in ``evaluator.py``. After a pipeline run
finishes, :func:`judge_run` asks Claude to grade the run on two dimensions:

  * ``clinical_soundness`` — is the Reconciler's risk / escalation / urgency
    decision well-calibrated and justified by the upstream agent findings (no
    alarm-fatigue over-escalation, no missed deterioration)? Deliberately does
    NOT see the ground-truth label — it judges the *reasoning*.
  * ``sbar_quality`` — is the clinician SBAR handoff complete, specific,
    urgency-aware, and free of hallucinated facts?

Results (label / score / explanation) are returned as :class:`EvalResult`s and
logged to Arize as eval feedback ON the run's trace span via
:func:`log_evals_to_arize`, so they appear next to the traces in app.arize.com.
Everything here is best-effort and never breaks the pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import config


@dataclass
class EvalResult:
    name: str
    label: str       # "good" | "needs_improvement"
    score: float     # 0.0 .. 1.0
    explanation: str

    def as_dict(self) -> dict:
        return asdict(self)


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction (handles ```json fences / surrounding prose)."""
    clean = re.sub(r"^```(?:json)?\s*", "", (text or "").strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```\s*$", "", clean, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except Exception:
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {}


_SYSTEM = (
    "You are an impartial clinical-QA evaluator for a post-discharge monitoring AI. "
    "You grade the AI's own output quality, not the patient. Be strict and specific. "
    "Return JSON only."
)


def _judge_call(client, rubric: str, payload: str, name: str) -> EvalResult:
    user = (
        f"{rubric}\n\nAI OUTPUT TO EVALUATE:\n{payload}\n\n"
        'Return JSON ONLY: {"label": "good" | "needs_improvement", '
        '"score": <float 0..1>, "explanation": "<1-2 sentences citing specifics>"}'
    )
    try:
        # Keep the judge's OWN LLM calls out of the trace data, so the Arize
        # project shows clean pipeline traces with eval labels — not meta noise.
        try:
            from openinference.instrumentation import suppress_tracing as _suppress
            _cm = _suppress()
        except Exception:
            import contextlib
            _cm = contextlib.nullcontext()
        with _cm:
            msg = client.messages.create(
                model=config.MODEL,
                max_tokens=600,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        d = _parse_json(text)
        label = str(d.get("label", "needs_improvement")).strip().lower().replace(" ", "_")
        if label not in ("good", "needs_improvement"):
            label = "needs_improvement"
        try:
            score = max(0.0, min(1.0, float(d.get("score", 0.0))))
        except (TypeError, ValueError):
            score = 0.0
        return EvalResult(name, label, score, str(d.get("explanation", ""))[:500])
    except Exception as exc:  # noqa: BLE001 - a judge failure must not break the run
        return EvalResult(name, "needs_improvement", 0.0, f"(judge error: {exc})")


def _agent_out(full_log: dict, agent_id: str) -> dict:
    return (full_log.get("agents", {}).get(agent_id, {}) or {}).get("full_output", {}) or {}


def _brief(full_log: dict, agent_id: str) -> dict:
    return (full_log.get("agents", {}).get(agent_id, {}) or {}).get("brief", {}) or {}


def judge_run(full_log: dict, *, client=None) -> list[EvalResult]:
    """Run both eval dimensions over one finished pipeline run. Never raises."""
    results: list[EvalResult] = []
    try:
        if client is None:
            client = config.get_anthropic_client()
    except Exception:  # noqa: BLE001 - no key / no client -> skip evals
        return results

    name = full_log.get("patient_name", "the patient")
    recon = _agent_out(full_log, "reconciler")

    # Dimension A — clinical soundness / calibration (NOT shown the ground truth).
    # Put the decision FIRST and compact the findings so the decision is never
    # truncated away (the findings briefs can be large).
    sig, trd, slf = _brief(full_log, "signal"), _brief(full_log, "trend"), _brief(full_log, "self_report")
    knw, skp = _brief(full_log, "knowledge"), _brief(full_log, "skeptic")
    compact_findings = {
        "signal": {k: sig.get(k) for k in ("severity", "summary", "news2_score", "news2_risk")},
        "trend": {k: trd.get(k) for k in ("severity", "summary", "concerning_trends", "weight_change", "steps_decline")},
        "self_report": {k: slf.get(k) for k in ("severity", "summary", "trajectory", "red_flags")},
        "knowledge": {k: knw.get(k) for k in ("severity", "summary", "triggered")},
        "skeptic": {"strongest_benign_case": skp.get("strongest_case"),
                    "where_skepticism_fails": skp.get("where_fails"),
                    "confidence": skp.get("confidence")},
    }
    soundness_payload = json.dumps({
        "reconciler_decision": {
            "risk_score_0_100": recon.get("risk_score"),
            "risk_level": recon.get("risk_level"),
            "escalation_level": recon.get("escalation_level"),
            "time_sensitivity": recon.get("time_sensitivity"),
            "primary_drivers": recon.get("primary_drivers"),
            "mitigating_factors": recon.get("mitigating_factors"),
            "rationale": recon.get("rationale"),
            "confidence": recon.get("confidence"),
        },
        "upstream_findings": compact_findings,
    }, indent=2, default=str)[:8000]
    soundness_rubric = (
        f"Evaluate the CLINICAL SOUNDNESS of the Reconciler's risk decision for {name}. "
        "Given the upstream agent findings, is the risk score / level / escalation / urgency "
        "well-calibrated and clearly justified by the evidence? Penalize over-escalation of a "
        "benign picture (alarm fatigue) AND under-escalation of genuine deterioration, and a "
        "rationale that does not follow from the cited findings. 'good' = well-calibrated and "
        "evidence-grounded; 'needs_improvement' otherwise."
    )
    results.append(_judge_call(client, soundness_rubric, soundness_payload, "clinical_soundness"))

    # Dimension B — SBAR handoff quality.
    sbar_payload = json.dumps({
        "sbar": full_log.get("sbar", ""),
        "reconciler_time_sensitivity": recon.get("time_sensitivity"),
        "reconciler_recommended_action": recon.get("recommended_action"),
    }, default=str)[:6000]
    sbar_rubric = (
        "Evaluate the QUALITY of this SBAR clinician handoff. Score on: all four sections present "
        "(Situation/Background/Assessment/Recommendation); recommendations are specific and "
        "actionable with concrete timeframes and numeric values; the urgency/time-sensitivity is "
        "EXPLICITLY reflected in the recommendations; and no facts are stated that aren't supported "
        "by the assessment (no hallucination). 'good' = complete, specific, urgency-aware, faithful; "
        "'needs_improvement' otherwise."
    )
    results.append(_judge_call(client, sbar_rubric, sbar_payload, "sbar_quality"))

    return results


def log_evals_to_arize(span_id: str | None, results: list[EvalResult]) -> bool:
    """Attach eval results to a span in Arize AX as feedback. Best-effort; never raises.

    Uses the verified API: ArizeClient(api_key=...).spans.update_evaluations(
    space_id, project_name, dataframe) where the dataframe has a `context.span_id`
    column plus `eval.<name>.label/.score/.explanation` columns.
    """
    from observability import tracing

    if not span_id or not results or tracing.backend() != "arize":
        return False
    try:
        import pandas as pd
        from arize.client import ArizeClient

        row: dict = {"context.span_id": span_id}
        for r in results:
            row[f"eval.{r.name}.label"] = r.label
            row[f"eval.{r.name}.score"] = float(r.score)
            row[f"eval.{r.name}.explanation"] = r.explanation
        df = pd.DataFrame([row])
        ArizeClient(api_key=config.ARIZE_API_KEY).spans.update_evaluations(
            space_id=config.ARIZE_SPACE_ID,
            project_name=config.ARIZE_PROJECT_NAME,
            dataframe=df,
        )
        return True
    except Exception:  # noqa: BLE001 - logging feedback must not break the run
        return False
