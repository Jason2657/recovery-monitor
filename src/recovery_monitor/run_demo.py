"""End-to-end demo entrypoint — runs the whole pipeline on a chosen profile.

    python -m recovery_monitor.run_demo --profile deterioration
    python -m recovery_monitor.run_demo --profile benign_decoy

For each daily decision point this:
  1. builds a PatientContext (latest reading + rolling window + self-reports),
  2. runs the three analysis agents CONCURRENTLY (asyncio.gather),
  3. computes the deterministic early-warning score,
  4. runs the Band room's bounded Skeptic<->Reconciler deliberation,
  5. applies the human-in-the-loop escalation gate,
  6. on escalate -> builds an SBAR brief and fires the escalation uAgent,
  7. ALWAYS updates the patient LCD (both paths),
all inside the Arize/Phoenix trace boundary. Finally it runs one pass of the
self-correction loop against the profile's ground-truth label.

With the stubbed agent logic this completes cleanly for both profiles — that is
the day-one integration guarantee. Real logic slots in behind the agent
interfaces without touching this orchestration.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from recovery_monitor import config
from recovery_monitor.agents.clinician_brief_agent import ClinicianBriefAgent
from recovery_monitor.agents.escalation_agent import EscalationAgent
from recovery_monitor.agents.selfreport_nlp_agent import SelfReportNLPAgent
from recovery_monitor.agents.signal_agent import SignalAgent
from recovery_monitor.agents.trend_agent import TrendAgent
from recovery_monitor.band.adapter import LocalBandRoom
from recovery_monitor.band.audit import AuditLog
from recovery_monitor.data import synthetic
from recovery_monitor.observability import evaluator, tracing
from recovery_monitor.schemas import GateDecision, PatientContext, RiskAssessment
from recovery_monitor.sim import sensors
from recovery_monitor.sim.lcd import LCD
from recovery_monitor.tools.early_warning import compute_early_warning

WINDOW_DAYS = 7


@dataclass
class TimestepResult:
    """One day's outcome — returned to callers (and asserted by the smoke test)."""

    day: int
    ews_band: str
    ews_score: int
    risk_score: float
    assessment: RiskAssessment
    decision: GateDecision
    escalated: bool


async def run_pipeline(
    profile: str,
    *,
    days: int = 7,
    speed: float = 0.0,
    patient_id: str = "patient-001",
    verbose: bool = True,
) -> list[TimestepResult]:
    """Run the full pipeline over ``profile`` and return per-day results."""
    tracing.init_tracing()

    # --- inputs: generate a labelled timeline, collapse to daily decisions ---
    hourly = synthetic.generate(profile, days=days)
    daily = synthetic.daily_summaries(hourly)
    daily_reports = [sensors.self_report_from_row(row) for _, row in daily.iterrows()]

    # --- wire the pipeline ---
    signal, trend, nlp = SignalAgent(), TrendAgent(), SelfReportNLPAgent()
    audit = AuditLog(echo=verbose)
    band = LocalBandRoom(audit=audit)  # swap for RemoteBandRoom once Band is wired
    clinician = ClinicianBriefAgent()
    escalation = EscalationAgent()
    lcd = LCD()

    window: list = []
    reports: list = []
    results: list[TimestepResult] = []

    if verbose:
        print(f"\n{'=' * 64}\n  Recovery Monitor — profile: {profile}\n{'=' * 64}")
        print("  (decision-support, not a diagnostic device)\n")

    with tracing.span("pipeline", profile=profile, patient_id=patient_id):
        idx = 0
        async for reading in sensors.stream(daily, speed=speed):
            day = idx + 1
            if daily_reports[idx] is not None:
                reports.append(daily_reports[idx])
            idx += 1

            window.append(reading)
            window = window[-WINDOW_DAYS:]
            ctx = PatientContext(
                patient_id=patient_id,
                latest=reading,
                rolling_window=list(window),
                self_reports=list(reports),
            )

            with tracing.span("timestep", day=day):
                # parallel analysis
                findings = list(
                    await asyncio.gather(
                        signal.evaluate(ctx),
                        trend.evaluate(ctx),
                        nlp.evaluate(ctx),
                    )
                )
                # deterministic early-warning score -> Reconciler
                ews = compute_early_warning(reading, list(window))
                # governed deliberation + human-in-the-loop gate
                assessment = await band.deliberate(findings, ews, ctx)
                decision = band.escalation_gate(assessment)

                if decision.escalate:
                    brief = await clinician.build(ctx, findings, assessment, ews)
                    await escalation.notify(brief, assessment)
                    lcd.show_notified()
                else:
                    lcd.show_ok()

            if verbose:
                print(
                    f"  Day {day} {reading.timestamp.date()}  "
                    f"HR {reading.heart_rate:5.1f}  mob {reading.mobility_index:.2f}  "
                    f"EWS {ews.band:>6}({ews.score})  risk {assessment.risk_score:.2f}  "
                    f"-> {'ESCALATE' if decision.escalate else 'hold'}\n"
                )

            results.append(
                TimestepResult(
                    day=day,
                    ews_band=ews.band,
                    ews_score=ews.score,
                    risk_score=assessment.risk_score,
                    assessment=assessment,
                    decision=decision,
                    escalated=decision.escalate,
                )
            )

    _run_correction_loop(profile, results, verbose=verbose)

    if verbose:
        escalated_days = [r.day for r in results if r.escalated]
        print(f"{'=' * 64}")
        print(f"  Done. {len(results)} days processed; {len(audit)} audit records written.")
        print(f"  Escalated on day(s): {escalated_days or 'none'}.")
        print(f"{'=' * 64}\n")

    return results


def _run_correction_loop(profile: str, results: list[TimestepResult], *, verbose: bool) -> None:
    """One pass of the Arize self-correction loop against ground truth."""
    if not results:
        return
    label = synthetic.PROFILE_LABELS.get(profile)
    if label is None:
        return
    final = results[-1].assessment
    score = evaluator.score_decision(final, label)
    before = config.get_risk_threshold()
    after = evaluator.adjust_threshold(final, label)
    if verbose:
        verdict = "correct" if (final.recommend_escalation == label) else "MISCALIBRATED"
        moved = "unchanged" if after == before else f"{before:.2f} -> {after:.2f}"
        print(
            f"  [self-correction] final decision was {verdict} "
            f"(calibration score {score:.2f}); RISK_THRESHOLD {moved}."
        )


def main() -> int:
    """CLI entrypoint (also the ``recovery-monitor-demo`` console script)."""
    parser = argparse.ArgumentParser(description="Run the recovery-monitor pipeline end-to-end.")
    parser.add_argument(
        "--profile",
        choices=sorted(synthetic.PROFILE_LABELS),
        default="deterioration",
        help="Which synthetic recovery timeline to run.",
    )
    parser.add_argument("--days", type=int, default=7, help="Days to simulate (5–7).")
    parser.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="Seconds between daily readings (0 = instant; try 0.4 for a live feel).",
    )
    parser.add_argument("--patient-id", default="patient-001")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-step output.")
    args = parser.parse_args()

    asyncio.run(
        run_pipeline(
            args.profile,
            days=args.days,
            speed=args.speed,
            patient_id=args.patient_id,
            verbose=not args.quiet,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
