#!/usr/bin/env python3
"""
PostCare AI Monitor — terminal runner (brief output only).
Full agent reasoning is saved to logs/. Run the web UI via: python3 app.py
"""

import os
import sys
import argparse
import anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.markdown import Markdown
from rich.markup import escape

from patient_data import PATIENTS, SENSOR_HISTORY, PATIENT_PROFILE

console = Console(highlight=False)


# ─── Color helpers ────────────────────────────────────────────────────────────

def sev_color(s: str) -> str:
    return {"low": "green", "medium": "yellow", "high": "orange1", "critical": "red"}.get(
        (s or "").lower(), "white"
    )


def score_color(n: int) -> str:
    if n < 30: return "green"
    if n < 55: return "yellow"
    if n < 75: return "orange1"
    return "red"


# ─── Brief display callbacks ──────────────────────────────────────────────────

AGENT_ICONS = {
    "signal": "⚡", "trend": "📈", "self_report": "💬",
    "knowledge": "📚", "skeptic": "⚔", "reconciler": "⚖", "brief": "📋",
}


def on_start(agent_id: str, label: str):
    icon = AGENT_ICONS.get(agent_id, "·")
    console.print(f"\n{icon}  [bold cyan]{label}[/bold cyan]", end="  ")


def on_complete(agent_id: str, label: str, brief: dict, elapsed: float):
    console.print(f"[dim]{elapsed}s[/dim]")

    if agent_id == "signal":
        sev = brief.get("severity", "unknown")
        sc = sev_color(sev)
        console.print(f"   NEWS2 [bold]{brief.get('news2_score', '?')}/15[/bold] — "
                      f"[{sc}]{brief.get('news2_risk', '?')} risk[/{sc}]  │  "
                      f"Severity: [{sc} bold]{sev.upper()}[/{sc} bold]")
        for a in brief.get("alerts", []):
            console.print(f"   [red]⚠  {escape(str(a))}[/red]")
        for c in brief.get("concerns", []):
            console.print(f"   [yellow]→  {escape(str(c))}[/yellow]")

    elif agent_id == "trend":
        sev = brief.get("severity", "unknown")
        sc = sev_color(sev)
        console.print(f"   Severity: [{sc} bold]{sev.upper()}[/{sc} bold]")
        for t in brief.get("concerning_trends", []):
            console.print(f"   [orange1]📈  {escape(str(t))}[/orange1]")

    elif agent_id == "self_report":
        sev = brief.get("severity", "unknown")
        traj = brief.get("trajectory", "unknown").replace("_", " ").upper()
        sc = sev_color(sev)
        console.print(f"   Trajectory: [{sc}]{traj}[/{sc}]  │  Severity: [{sc} bold]{sev.upper()}[/{sc} bold]")
        for f in brief.get("red_flags", []):
            console.print(f'   [italic yellow]"{escape(str(f))}"[/italic yellow]')

    elif agent_id == "knowledge":
        sev = brief.get("severity", "unknown")
        sc = sev_color(sev)
        console.print(f"   Severity: [{sc} bold]{sev.upper()}[/{sc} bold]")
        for t in brief.get("triggered", []):
            console.print(f"   [red]✓  {escape(str(t))}[/red]")

    elif agent_id == "skeptic":
        conf = brief.get("confidence", "?")
        console.print(f"   Benign confidence: [green]{conf}[/green]")
        strongest = brief.get("strongest_case", "")
        if strongest:
            console.print(f"   [dim]Best benign: {escape(strongest[:120])}{'…' if len(strongest) > 120 else ''}[/dim]")
        fails = brief.get("where_fails", "")
        if fails:
            console.print(f"   [yellow]Skepticism fails: {escape(fails[:120])}{'…' if len(fails) > 120 else ''}[/yellow]")

    elif agent_id == "reconciler":
        score = brief.get("risk_score", 0)
        level = brief.get("risk_level", "unknown").upper()
        rc = score_color(score)
        action = brief.get("recommended_action", "")
        timing = str(brief.get("time_sensitivity", "")).replace("_", " ").upper()
        console.print(f"   Risk: [{rc} bold]{score}/100 — {level}[/{rc} bold]  │  Confidence: {brief.get('confidence', '?')}")
        for d in brief.get("primary_drivers", []):
            console.print(f"   [{rc}]▶  {escape(str(d))}[/{rc}]")
        console.print(f"   [bold]Action:[/bold] {escape(action)}  │  [{rc}]{timing}[/{rc}]")

    elif agent_id == "brief":
        pass  # SBAR printed separately at end


# ─── Patient & sensor display ─────────────────────────────────────────────────

def display_patient_card(profile: dict, history: list):
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column(style="bold cyan", width=22)
    t.add_column()
    t.add_row("Patient", f"{profile['name']}  ·  {profile['age']}yo {profile['sex']}")
    t.add_row("Diagnosis", profile["diagnosis"])
    t.add_row("Discharge Date", f"{profile['discharge_date']}  (Day {len(history)} today)")
    t.add_row("Medications", "  ·  ".join(profile["medications"]))
    t.add_row("Baseline 30-day Risk", f"[red bold]{profile['30day_readmission_risk']}[/red bold]")
    console.print(Panel(t, title="[bold]Patient Profile[/bold]", border_style="cyan"))


def display_sensor_summary(history: list):
    d = history[-1]
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold dim", width=20)
    t.add_column()
    t.add_column(style="bold dim", width=20)
    t.add_column()

    hr_c = "green" if d["hr_resting_bpm"] <= 80 else ("yellow" if d["hr_resting_bpm"] <= 90 else "red")
    sp_c = "green" if d["spo2_pct"] >= 96 else ("yellow" if d["spo2_pct"] >= 93 else "red")
    wt_delta = round(d["weight_lbs"] - history[0]["weight_lbs"], 1)
    wt_c = "green" if wt_delta < 3 else ("yellow" if wt_delta < 5 else "red")
    tc = "green" if d["temp_c"] < 37.5 else ("yellow" if d["temp_c"] < 38.0 else "red")

    t.add_row("HR (resting)", f"[{hr_c}]{d['hr_resting_bpm']} bpm[/{hr_c}]",
              "SpO2", f"[{sp_c}]{d['spo2_pct']}%[/{sp_c}]")
    t.add_row("Weight (Δ from baseline)", f"[{wt_c}]+{wt_delta} lbs[/{wt_c}]",
              "Temperature", f"[{tc}]{d['temp_c']}°C[/{tc}]")
    t.add_row("Steps today", str(d["steps"]),
              "Night wakes", str(d["sleep_interruptions"]))
    console.print(Panel(t, title=f"[bold]Today's Snapshot (Day {d['day']})[/bold]", border_style="dim"))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red bold]Error:[/red bold] ANTHROPIC_API_KEY not set. Copy .env.example → .env")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="PostCare AI Monitor — Terminal Runner")
    parser.add_argument("--patient", default="PT-7421", choices=list(PATIENTS.keys()),
                        help="Patient ID to analyze")
    parser.add_argument("--list", action="store_true", help="List available patients")
    parser.add_argument("--log-dir", default="logs", help="Directory for full reasoning logs")
    args = parser.parse_args()

    if args.list:
        console.print("\n[bold]Available Patients:[/bold]")
        for pid, pdata in PATIENTS.items():
            p = pdata["profile"]
            console.print(f"  {pid}  {p['name']}, {p['age']}yo — {p['diagnosis']}")
        return

    patient_data = PATIENTS[args.patient]
    profile = patient_data["profile"]
    history = patient_data["sensor_history"]
    reports = patient_data["self_reports"]

    # Header
    console.print()
    header = Table.grid(expand=True)
    header.add_column(justify="center")
    header.add_row("[bold white]PostCare AI Monitor[/bold white]")
    header.add_row("[dim]Multi-Agent Post-Discharge Surveillance · Brief Output Mode[/dim]")
    console.print(Panel(header, border_style="blue", padding=(1, 4)))

    console.print()
    display_patient_card(profile, history)
    console.print()
    display_sensor_summary(history)
    console.print()
    console.print(f'[dim italic]Today\'s check-in: "{escape(reports[-1]["text"][:120])}…"[/dim italic]')
    console.print()
    console.print(Rule("[bold]Running 7-Agent Pipeline[/bold]  [dim](full reasoning → logs/)[/dim]", style="blue"))

    from agents import run_full_pipeline

    client = anthropic.Anthropic(api_key=api_key)
    full_log, log_file = run_full_pipeline(
        patient_data, client,
        callbacks={"on_start": on_start, "on_complete": on_complete},
        log_dir=args.log_dir,
    )

    # Final risk banner
    score = full_log.get("risk_score", 0)
    level = full_log.get("risk_level", "unknown").upper()
    rc = score_color(score)
    action = full_log.get("recommended_action", "N/A")
    timing = str(full_log.get("time_sensitivity", "N/A")).replace("_", " ").upper()

    console.print()
    console.print(Panel(
        f"[{rc} bold]RISK SCORE:  {score}/100 — {level}[/{rc} bold]\n"
        f"[bold]Action:[/bold]  [{rc}]{escape(action)}[/{rc}]\n"
        f"[bold]Urgency:[/bold] [{rc} bold]{timing}[/{rc} bold]",
        title="[bold]Final Assessment[/bold]",
        border_style=rc,
        padding=(1, 3),
    ))

    # SBAR
    sbar = full_log.get("sbar", "")
    if sbar:
        console.print()
        console.print(Rule("[bold blue]SBAR Clinical Brief[/bold blue]", style="blue"))
        console.print(Panel(Markdown(sbar), border_style="blue", padding=(1, 2)))

    console.print()
    console.print(f"[dim]Full reasoning log → [bold]{log_file}[/bold][/dim]")
    console.print()


if __name__ == "__main__":
    main()
