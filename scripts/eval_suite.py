#!/usr/bin/env python3
"""Arize eval suite — run the LLM judge across all patients, log to Arize, summarize.

    python scripts/eval_suite.py --tag baseline
    python scripts/eval_suite.py --tag improved --patients PT-7421,PT-2048

Each patient runs the full pipeline with the LLM-as-judge ON, so every run:
  * sends a trace (pipeline + agent.* + auto-instrumented Claude spans) to Arize, and
  * attaches clinical_soundness + sbar_quality eval labels to that trace's span.

It then aggregates the eval scores and writes logs/eval_suite_<tag>_<ts>.json, so you
can compare baseline vs improved (and watch the eval-score distribution shift in
Arize over time). This is the artifact judges inspect at the booth.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from observability import tracing  # noqa: E402
from patient_data import PATIENTS  # noqa: E402
from agents import run_full_pipeline  # noqa: E402

try:
    from rich.console import Console
    from rich.table import Table

    console: Console | None = Console()
except Exception:  # pragma: no cover
    console = None


def _p(msg: str) -> None:
    console.print(msg) if console else print(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Arize LLM-judge eval suite")
    ap.add_argument("--tag", default="baseline", help="label for this run (e.g. baseline / improved)")
    ap.add_argument("--patients", default="", help="comma-separated patient ids (default: all)")
    args = ap.parse_args()

    try:
        client = config.get_anthropic_client()
    except RuntimeError as e:
        _p(f"Error: {e}")
        return 1

    ids = [p.strip() for p in args.patients.split(",") if p.strip()] or list(PATIENTS.keys())
    tracing.init_tracing()
    _p(f"Tracing backend: [bold]{tracing.backend()}[/bold]  (Arize project: {config.ARIZE_PROJECT_NAME})")
    _p(f"Running {len(ids)} patient(s) through pipeline + LLM judge -> Arize  [tag={args.tag}]\n")

    rows = []
    for pid in ids:
        if pid not in PATIENTS:
            _p(f"  skip {pid} (unknown)")
            continue
        _p(f"  > {pid}  {PATIENTS[pid]['profile']['name']} ...")
        full_log = None
        for attempt in (1, 2, 3):  # transient API/network blips shouldn't kill the suite
            try:
                full_log, _ = run_full_pipeline(PATIENTS[pid], client, log_dir="logs")  # judge ON by default
                break
            except Exception as exc:
                _p(f"    [attempt {attempt}/3] error: {str(exc)[:120]}")
        if full_log is None:
            rows.append({"patient_id": pid, "name": PATIENTS[pid]["profile"]["name"],
                         "risk_score": None, "gate_escalate": None, "evals_logged": False,
                         "clinical_soundness": {}, "sbar_quality": {}, "error": "pipeline failed"})
            continue
        evals = {e["name"]: e for e in full_log.get("llm_evals", [])}
        rows.append({
            "patient_id": pid,
            "name": PATIENTS[pid]["profile"]["name"],
            "risk_score": full_log.get("risk_score"),
            "gate_escalate": full_log.get("gate_decision", {}).get("escalate"),
            "evals_logged": full_log.get("evals_logged"),
            "clinical_soundness": evals.get("clinical_soundness", {}),
            "sbar_quality": evals.get("sbar_quality", {}),
        })

    def mean(dim: str):
        vals = [r[dim].get("score") for r in rows if r[dim].get("score") is not None]
        return round(statistics.mean(vals), 3) if vals else None

    agg = {"clinical_soundness_mean": mean("clinical_soundness"), "sbar_quality_mean": mean("sbar_quality")}
    needs = [
        {"patient_id": r["patient_id"], "dimension": d, "explanation": r[d].get("explanation", "")}
        for r in rows for d in ("clinical_soundness", "sbar_quality")
        if r[d].get("label") == "needs_improvement"
    ]

    if console:
        t = Table(title=f"Arize eval suite — {args.tag}")
        t.add_column("Patient", style="bold")
        t.add_column("Risk")
        t.add_column("clinical_soundness")
        t.add_column("sbar_quality")
        t.add_column("→Arize", justify="center")
        for r in rows:
            cs, sb = r["clinical_soundness"], r["sbar_quality"]
            csc = "green" if cs.get("label") == "good" else "yellow"
            sbc = "green" if sb.get("label") == "good" else "yellow"
            t.add_row(
                f"{r['patient_id']} {r['name']}", str(r["risk_score"]),
                f"[{csc}]{cs.get('label', '?')} ({cs.get('score', '?')})[/{csc}]",
                f"[{sbc}]{sb.get('label', '?')} ({sb.get('score', '?')})[/{sbc}]",
                "✓" if r["evals_logged"] else "—",
            )
        console.print(t)
        console.print(f"\n[bold]Means[/bold] — clinical_soundness: [cyan]{agg['clinical_soundness_mean']}[/cyan]  |  "
                      f"sbar_quality: [cyan]{agg['sbar_quality_mean']}[/cyan]")
        if needs:
            console.print("\n[yellow]needs_improvement flags (the judge's feedback):[/yellow]")
            for n in needs:
                console.print(f"  {n['patient_id']} · {n['dimension']}: {n['explanation'][:170]}")
    else:
        print(json.dumps({"aggregate": agg, "rows": rows}, indent=2, default=str))

    os.makedirs("logs", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join("logs", f"eval_suite_{args.tag}_{ts}.json")
    with open(out, "w") as f:
        json.dump({"tag": args.tag, "aggregate": agg, "needs_improvement": needs, "rows": rows}, f, indent=2, default=str)
    _p(f"\nSaved summary -> {out}")
    _p(f"View traces + eval feedback -> app.arize.com  (project: {config.ARIZE_PROJECT_NAME})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
