"""Signal agent — acute anomaly detection on the latest reading.

Owner: Person C (Sensing & scoring).

Scope: point-in-time anomalies (a sudden HR spike, a hard mobility drop, a
missed check-in) — the "something is wrong RIGHT NOW" signal, as opposed to the
Trend agent's slow-drift view.
"""

from __future__ import annotations

from recovery_monitor.agents.base import BaseAgent
from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import AgentFinding, PatientContext


class SignalAgent(BaseAgent):
    """Flags acute, single-reading anomalies."""

    def __init__(self) -> None:
        super().__init__(name="signal_agent")

    @traced("signal_agent.evaluate")
    async def evaluate(self, ctx: PatientContext) -> AgentFinding:
        """Detect acute anomalies in ``ctx.latest``.

        STUB: returns a fixed low-severity finding so the pipeline runs end-to-end.

        TODO(sensing-owner): implement acute detection, e.g.
          * heart rate outside a personalized resting band (z-score vs. window),
          * abrupt mobility_index collapse vs. the patient's recent baseline,
          * a missed touch check-in at an expected time.
        Set ``severity`` from how far out-of-band the reading is and put the
        triggering numbers in ``evidence``.
        """
        r = ctx.latest
        return AgentFinding(
            agent_name=self.name,
            summary="(stub) no acute anomaly detected in latest reading",
            severity=0.1,
            evidence={
                "heart_rate": r.heart_rate,
                "mobility_index": r.mobility_index,
                "touch_checkin": r.touch_checkin,
            },
        )
