"""Band backend adapter — swap the in-process room for the real Band API.

The pipeline depends on the ``BandRoomBackend`` protocol, never on a concrete
backend. ``LocalBandRoom`` (default) runs the governance logic in-process;
``RemoteBandRoom`` is the stub for the real Band service, to be filled in once
integration is confirmed. Keep this boundary clean — it is the whole point of the
adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from recovery_monitor.band.audit import AuditLog
from recovery_monitor.band.room import Authorizer, BandRoom
from recovery_monitor.schemas import (
    AgentFinding,
    EarlyWarningScore,
    GateDecision,
    PatientContext,
    RiskAssessment,
)


@runtime_checkable
class BandRoomBackend(Protocol):
    """The interface the pipeline talks to. Implemented by any Band backend."""

    async def deliberate(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        context: PatientContext,
    ) -> RiskAssessment: ...

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision: ...


class LocalBandRoom:
    """Default in-process backend — delegates to a local :class:`BandRoom`.

    Fast, dependency-free, deterministic; ideal for the demo and tests.
    """

    def __init__(
        self,
        audit: AuditLog | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        self.audit = audit or AuditLog()
        self._room = BandRoom(audit=self.audit, authorizer=authorizer)

    async def deliberate(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        context: PatientContext,
    ) -> RiskAssessment:
        return await self._room.deliberate(findings, ews, context)

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        return self._room.escalation_gate(assessment)


class RemoteBandRoom:
    """Stub for the real Band API backend.

    TODO(governance-owner): implement against the Band service —
      * authenticate with BAND_API_KEY (see config),
      * open/join a governed room for the patient,
      * submit findings + EWS and run the verified-authority deliberation
        server-side,
      * stream Band's audit trail back into our AuditLog (or read it from Band).
    Until then this raises so nobody silently ships against an empty backend.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def deliberate(
        self,
        findings: list[AgentFinding],
        ews: EarlyWarningScore,
        context: PatientContext,
    ) -> RiskAssessment:
        raise NotImplementedError(
            "RemoteBandRoom.deliberate is not implemented yet — use LocalBandRoom "
            "for now. See TODO(governance-owner)."
        )

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        raise NotImplementedError(
            "RemoteBandRoom.escalation_gate is not implemented yet — use LocalBandRoom."
        )
