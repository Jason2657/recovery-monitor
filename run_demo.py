#!/usr/bin/env python3
"""PostCare AI Monitor — end-to-end demo that surfaces all three sponsor tracks.

    python run_demo.py --patient PT-7421            # direct
    python run_demo.py --patient PT-7421 --mesh     # parallel analysis via Fetch.ai uAgents
    python run_demo.py --patient PT-2048            # benign decoy (Skeptic should win)
    python run_demo.py --list

What it shows, beyond the 7-agent reasoning:
  * Fetch.ai — the three analysis agents as real uAgents (with --mesh), plus their
    addresses; the Escalation uAgent on the output path.
  * Band     — the governed escalation gate decision + the append-only audit trail.
  * Arize    — whether the trace boundary is live (Phoenix), the trace id, and one
    pass of the self-correction loop nudging RISK_THRESHOLD against ground truth.
"""

from __future__ import annotations

import argparse
import sys

import config
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.markdown import Markdown
from rich.markup import escape

from patient_data import PATIENTS
from agents import run_full_pipeline
from observability import tracing
from mesh import fetch_mesh

# Reuse the terminal UI built in main.py.
from main import (
    console,
    on_start,
    on_complete,
    display_patient_card,
    display_sensor_summary,
    score_color,
)


def _sponsor_banner(use_mesh: bool) -> None:
    tracing.init_tracing()  # so we can report whether Phoenix/Arize is live
    fetch_state = (
        "[green]available[/green]" if fetch_mesh.is_available() else "[yellow]not installed[/yellow]"
    )
    arize_state = (
        "[green]live (Phoenix/Arize)[/green]" if tracing.is_enabled() else "[yellow]no-op (no collector)[/yellow]"
    )
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Fetch.ai uAgents", f"{fetch_state}" + (" · routing analysis via mesh" if use_mesh else " · direct mode"))
    t.add_row("Band governance", "[green]LocalBandRoom[/green] (audit + human-in-the-loop gate)")
    t.add_row("Arize / Phoenix", arize_state)
    console.print(Panel(t, title="[bold]Sponsor stack[/bold]", border_style="magenta"))

    if use_mesh and fetch_mesh.is_available():
        mt = Table(show_header=True, box=None, padding=(0, 2))
        mt.add_column("uAgent", style="bold cyan")
        mt.add_column("address", style="dim")
        for s in fetch_mesh.mesh_status():
            mt.add_row(s["name"], s["address"])
        console.print(Panel(mt, title="[bold]Fetch.ai mesh — real uAgents[/bold]", border_style="cyan"))


def _governance_panel(full_log: dict) -> None:
    gate = full_log.get("gate_decision", {})
    audit = full_log.get("band_audit", [])

    escalated = gate.get("escalate")
    head = "[red bold]ESCALATE[/red bold]" if escalated else "[green bold]HOLD[/green bold]"
    console.print(
        Panel(
            f"{head}  —  authority "
            f"{'granted' if gate.get('authority_granted') else 'denied'}\n"
            f"[dim]{escape(str(gate.get('reason', '')))}[/dim]",
            title="[bold]Band — escalation gate[/bold]",
            border_style="red" if escalated else "green",
        )
    )

    at = Table(show_header=True, box=None, padding=(0, 2))
    at.add_column("#", style="dim", width=3)
    at.add_column("actor", style="bold")
    at.add_column("action")
    at.add_column("authority", justify="center")
    for i, rec in enumerate(audit, 1):
        auth = "[green]yes[/green]" if rec.get("authority_granted") else "[dim]—[/dim]"
        at.add_row(str(i), str(rec.get("actor", "")), str(rec.get("action", "")), auth)
    console.print(Panel(at, title=f"[bold]Band — append-only audit trail ({len(audit)} records)[/bold]", border_style="blue"))


def _arize_panel(full_log: dict) -> None:
    sc = full_log.get("self_correction", {})
    trace_id = full_log.get("trace_id")
    lines = []
    lines.append(
        f"Tracing: {'[green]live[/green]' if full_log.get('tracing_enabled') else '[yellow]no-op[/yellow]'}"
        + (f"  ·  trace_id [dim]{trace_id}[/dim]" if trace_id else "")
    )
    if sc.get("ran"):
        verdict = "[green]correct[/green]" if sc.get("correct") else "[red]MISCALIBRATED[/red]"
        before, after = sc.get("threshold_before"), sc.get("threshold_after")
        moved = "unchanged" if before == after else f"{before:.2f} → {after:.2f}"
        lines.append(
            f"Self-correction: predicted "
            f"{'escalate' if sc.get('predicted_escalate') else 'hold'} vs truth "
            f"{'escalate' if sc.get('ground_truth_escalate') else 'hold'} → {verdict}"
        )
        lines.append(
            f"RISK_THRESHOLD: [bold]{moved}[/bold]  (calibration score {sc.get('calibration_score')})"
        )
    else:
        lines.append(f"Self-correction: [dim]{escape(str(sc.get('reason', 'n/a')))}[/dim]")
    console.print(Panel("\n".join(lines), title="[bold]Arize — observability + self-correction[/bold]", border_style="magenta"))


def main() -> int:
    parser = argparse.ArgumentParser(description="PostCare AI Monitor — sponsor-aware demo")
    parser.add_argument("--patient", default="PT-7421", choices=list(PATIENTS.keys()))
    parser.add_argument("--mesh", action="store_true", help="Route parallel analysis through the Fetch.ai uAgents mesh")
    parser.add_argument("--list", action="store_true", help="List available patients")
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args()

    if args.list:
        console.print("\n[bold]Available patients:[/bold]")
        for pid, pdata in PATIENTS.items():
            p = pdata["profile"]
            gt = p.get("ground_truth_escalate")
            tag = "" if gt is None else ("  [dim](truth: escalate)[/dim]" if gt else "  [dim](truth: benign)[/dim]")
            console.print(f"  {pid}  {p['name']}, {p['age']}yo — {p['diagnosis']}{tag}")
        return 0

    try:
        client = config.get_anthropic_client()
    except RuntimeError as exc:
        console.print(f"[red bold]Error:[/red bold] {exc}")
        return 1

    patient = PATIENTS[args.patient]
    profile, history, reports = patient["profile"], patient["sensor_history"], patient["self_reports"]

    console.print()
    console.print(Panel("[bold white]PostCare AI Monitor[/bold white]\n"
                        "[dim]Governed multi-agent post-discharge surveillance — decision-support, not a diagnostic device[/dim]",
                        border_style="blue", padding=(1, 4)))
    _sponsor_banner(args.mesh)
    console.print()
    display_patient_card(profile, history)
    console.print()
    display_sensor_summary(history)
    console.print()
    console.print(f'[dim italic]Today\'s check-in: "{escape(reports[-1]["text"][:120])}…"[/dim italic]')
    console.print()
    console.print(Rule("[bold]Running governed 7-agent pipeline[/bold]", style="blue"))

    full_log, log_file = run_full_pipeline(
        patient, client,
        callbacks={"on_start": on_start, "on_complete": on_complete},
        log_dir=args.log_dir,
        use_mesh=args.mesh,
    )
    if full_log.get("mesh_error"):
        console.print(f"[yellow]mesh fell back to direct calls:[/yellow] {full_log['mesh_error']}")

    # Final risk banner
    score = full_log.get("risk_score", 0)
    level = str(full_log.get("risk_level", "unknown")).upper()
    rc = score_color(score)
    console.print()
    console.print(Panel(
        f"[{rc} bold]RISK SCORE:  {score}/100 — {level}[/{rc} bold]\n"
        f"[bold]Action:[/bold]  [{rc}]{escape(str(full_log.get('recommended_action', 'N/A')))}[/{rc}]\n"
        f"[bold]Urgency:[/bold] [{rc} bold]{str(full_log.get('time_sensitivity', 'N/A')).replace('_', ' ').upper()}[/{rc} bold]",
        title="[bold]Final Assessment[/bold]", border_style=rc, padding=(1, 3),
    ))

    # Sponsor surfaces
    console.print()
    _governance_panel(full_log)
    console.print()
    _arize_panel(full_log)

    # SBAR
    sbar = full_log.get("sbar", "")
    if sbar:
        console.print()
        console.print(Rule("[bold blue]SBAR Clinical Brief[/bold blue]", style="blue"))
        console.print(Panel(Markdown(sbar), border_style="blue", padding=(1, 2)))

    console.print()
    console.print(f"[dim]Full reasoning + audit log → [bold]{log_file}[/bold][/dim]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
