"""Escalation agent — the Fetch.ai uAgent that notifies a care coordinator.

Owner: Person D (Language & handoff).

This is the output action on the "risk high" fork: take the SBAR brief and get it
in front of a human care coordinator. In the demo it prints to console; the real
version is a Fetch.ai uAgent that messages the coordinator's agent (or a
pager/SMS/Slack bridge).
"""

from __future__ import annotations

from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import RiskAssessment, SBARBrief


class EscalationAgent:
    """Delivers the clinical handoff to a care coordinator (Fetch.ai uAgent)."""

    name = "escalation_agent"

    def __init__(self, coordinator_address: str | None = None) -> None:
        #: Mesh address of the care-coordinator's uAgent (None in the local demo).
        self.coordinator_address = coordinator_address

    @traced("escalation_agent.notify")
    async def notify(self, brief: SBARBrief, assessment: RiskAssessment) -> bool:
        """Notify the care coordinator with the SBAR brief.

        STUB: prints the handoff to the console and returns True.

        TODO(fetch-owner): make this a real Fetch.ai uAgent send — wrap with
        ``BaseAgent.as_uagent`` patterns (or a dedicated bureau agent), define an
        ``SBARMessage`` model, and ``await ctx.send(self.coordinator_address, ...)``.
        Add delivery confirmation + retry, and record the send in the Band audit.
        """
        print("\n" + "=" * 64)
        print("  🚨 ESCALATION — care coordinator notified (SBAR)")
        print("=" * 64)
        print(f"  Risk score : {assessment.risk_score:.0%}")
        print(f"  S (Situation)     : {brief.situation}")
        print(f"  B (Background)     : {brief.background}")
        print(f"  A (Assessment)     : {brief.assessment}")
        print(f"  R (Recommendation) : {brief.recommendation}")
        print("=" * 64 + "\n")
        return True

    def as_uagent(self, seed: str | None = None):
        """Return the Fetch.ai ``uagents.Agent`` for the escalation sender.

        Stubbed bridge (lazy ``uagents`` import) so the local demo runs without
        the dependency. TODO(fetch-owner): register on the bureau and wire the
        outbound SBAR message.
        """
        try:
            from uagents import Agent
        except ImportError as exc:  # pragma: no cover - optional until mesh lands
            raise RuntimeError(
                "uagents is not installed. Run `uv sync` to use the Fetch.ai mesh."
            ) from exc
        return Agent(name=self.name, seed=seed or "recovery-escalation")
