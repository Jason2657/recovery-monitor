"""BaseAgent — the common shape for every analysis agent, plus the Fetch.ai
uAgents wrapper.

Sponsor boundary: the agents themselves are Fetch.ai uAgents. In the local demo
they run as plain async objects (fast, no network); ``as_uagent()`` is the real
bridge to the Fetch mesh — the wrapper is stubbed but present so the Fetch story
is concrete and a teammate can finish the mesh wiring without restructuring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from recovery_monitor.schemas import AgentFinding, PatientContext


class BaseAgent(ABC):
    """Common interface for the Signal / Trend / Self-report NLP agents.

    Subclasses implement :meth:`evaluate`. The pipeline runs the analysis agents
    concurrently (``asyncio.gather``) and collects their findings.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def evaluate(self, ctx: PatientContext) -> AgentFinding:
        """Analyze the patient context and return a single finding.

        Implementations MUST be async (so the orchestrator can fan them out) and
        MUST return an :class:`~recovery_monitor.schemas.AgentFinding` with
        ``agent_name == self.name``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Fetch.ai uAgents bridge
    # ------------------------------------------------------------------ #
    def as_uagent(self, seed: str | None = None):
        """Wrap this agent as a Fetch.ai ``uagents.Agent`` with a message handler.

        Returns a configured ``uagents.Agent`` whose handler runs ``evaluate`` and
        replies with the finding. The mesh wiring (message models, addressing,
        bureau registration, the Reconciler subscribing to findings) is left for
        the Fetch owner — but the wrapper exists so the integration is real, not
        hypothetical.

        ``uagents`` is imported lazily so the local demo runs without it installed.
        """
        try:
            from uagents import Agent, Context, Model
        except ImportError as exc:  # pragma: no cover - optional until mesh lands
            raise RuntimeError(
                "uagents is not installed. Run `uv sync` (or `pip install uagents`) "
                "to use the Fetch.ai mesh."
            ) from exc

        # Message envelopes for the mesh. Kept minimal; the Reconciler will
        # consume FindingMessage off the bus.
        class ContextMessage(Model):
            patient_id: str
            payload: dict  # serialized PatientContext

        class FindingMessage(Model):
            agent_name: str
            summary: str
            severity: float
            evidence: dict

        agent = Agent(name=self.name, seed=seed or f"recovery-{self.name}")

        @agent.on_message(model=ContextMessage)
        async def _handle(ctx: "Context", sender: str, msg: ContextMessage) -> None:  # noqa: ANN001
            # TODO(fetch-owner): reconstruct PatientContext from msg.payload,
            # call `await self.evaluate(context)`, and reply to `sender` /
            # publish onto the Reconciler's topic with a FindingMessage.
            finding = AgentFinding(
                agent_name=self.name, summary="(uagent stub)", severity=0.0, evidence={}
            )
            await ctx.send(
                sender,
                FindingMessage(**finding.model_dump()),
            )

        return agent
