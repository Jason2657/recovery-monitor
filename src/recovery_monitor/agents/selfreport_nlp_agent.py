"""Self-report NLP agent — vague patient language -> structured symptom signal.

Owner: Person D (Language & handoff).

Scope: turn a free-text/voice daily note ("a bit more tired today, didn't sleep
great") into a structured symptom severity the Reconciler can weigh. This is one
of the three Claude-backed agents.
"""

from __future__ import annotations

from recovery_monitor.agents.base import BaseAgent
from recovery_monitor.observability.tracing import traced
from recovery_monitor.schemas import AgentFinding, PatientContext


class SelfReportNLPAgent(BaseAgent):
    """Extracts a structured symptom signal from the latest self-report."""

    def __init__(self) -> None:
        super().__init__(name="selfreport_nlp_agent")

    @traced("selfreport_nlp_agent.evaluate")
    async def evaluate(self, ctx: PatientContext) -> AgentFinding:
        """Map the most recent self-report to a structured symptom finding.

        STUB: returns a fixed low-severity finding so the pipeline runs end-to-end.

        TODO(language-owner): call Claude (via ``config.get_anthropic_client()``)
        to extract structured symptoms from ``latest_report.raw_text``:
          * normalized symptom tags (pain, fatigue, nausea, fever, wound issues),
          * a severity 0–1 and a trend hint vs. earlier reports,
          * salient quotes -> ``evidence`` (also useful for the SBAR handoff).
        Keep the prompt deterministic (low temperature) and force structured
        (JSON / tool-use) output so the Reconciler gets a clean signal.
        """
        latest_report = ctx.self_reports[-1] if ctx.self_reports else None
        return AgentFinding(
            agent_name=self.name,
            summary="(stub) no symptom extraction performed",
            severity=0.1,
            evidence={
                "raw_text": latest_report.raw_text if latest_report else None,
                "report_count": len(ctx.self_reports),
            },
        )
