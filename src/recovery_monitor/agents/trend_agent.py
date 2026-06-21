"""Trend agent — rolling 5–7 day drift detection. THE INTELLECTUAL CORE.

Owner: Person A (Drift & data) — ships together with ``data/synthetic.py``; the
agent is only as good as the timelines it is tested against.

Why this is the core: post-op deterioration at home is usually *slow*. A single
reading looks fine; the danger is the trajectory. This agent's whole job is to
catch the creep that point-in-time checks miss.

Target signals to detect across the rolling window (``ctx.rolling_window``):
  * resting heart rate creeping up (~+2 bpm/day is a meaningful drift),
  * rising night-time light-on frequency (sleep fragmenting — ``ambient_light``
    elevated overnight on more days),
  * mobility_index trending downward day over day (the validated readmission
    predictor for colorectal recovery).
Combine the slopes into a single severity; the Reconciler weighs it against the
Skeptic's benign explanations.
"""

from __future__ import annotations

from recovery_monitor.agents.base import BaseAgent
from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import AgentFinding, PatientContext


class TrendAgent(BaseAgent):
    """Detects slow multi-day deterioration in the rolling window."""

    def __init__(self) -> None:
        super().__init__(name="trend_agent")

    @traced("trend_agent.evaluate")
    async def evaluate(self, ctx: PatientContext) -> AgentFinding:
        """Estimate deterioration drift over ``ctx.rolling_window``.

        STUB: returns a fixed low-severity finding so the pipeline runs end-to-end.

        TODO(drift-owner): implement the real drift detection:
          1. Fit a slope (e.g. linear regression / Theil–Sen) per signal over the
             window: resting HR, mobility_index, night-time ambient_light.
          2. Normalize each slope to a 0–1 concern (HR up = bad, mobility down =
             bad, night light up = bad).
          3. Fuse into ``severity`` and record the slopes + window length in
             ``evidence`` so the Reconciler and audit trail can see the math.
        Needs >= ~3 days of window to be meaningful — return low severity and say
        so when the window is too short.
        """
        window = ctx.rolling_window
        return AgentFinding(
            agent_name=self.name,
            summary=f"(stub) no drift computed yet over {len(window)} readings",
            severity=0.1,
            evidence={
                "window_size": len(window),
                "hr_slope_bpm_per_day": None,  # TODO(drift-owner): compute
                "mobility_slope_per_day": None,  # TODO(drift-owner): compute
                "night_light_trend": None,  # TODO(drift-owner): compute
            },
        )
