"""Band backend adapter — swap the in-process room for a real Band service.

The pipeline depends on the ``BandRoomBackend`` protocol, never on a concrete
backend. ``LocalBandRoom`` (default) runs the governance logic in-process;
``RemoteBandRoom`` is the seam where a real Band API/SDK plugs in. Keep this
boundary clean — it is the whole point of the adapter.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

import config
from band.audit import AuditLog
from band.room import Authorizer, BandRoom, DeliberationResult
from schemas import GateDecision, RiskAssessment


@runtime_checkable
class BandRoomBackend(Protocol):
    """The interface the pipeline talks to. Implemented by any Band backend."""

    audit: AuditLog

    def deliberate(
        self,
        *,
        patient_id: str,
        analysis_summary: dict,
        skeptic_call: Callable[[], dict],
        reconciler_call: Callable[[dict], dict],
        trace_id: str | None = ...,
    ) -> DeliberationResult: ...

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
        self.audit = audit if audit is not None else AuditLog()
        self._room = BandRoom(audit=self.audit, authorizer=authorizer)

    def deliberate(self, **kwargs) -> DeliberationResult:
        return self._room.deliberate(**kwargs)

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        return self._room.escalation_gate(assessment)


class RemoteBandRoom:
    """Stub for a real Band service backend.

    TODO(governance-owner): implement against the Band service/SDK —
      * authenticate with BAND_API_KEY (see config),
      * open/join a governed room for the patient,
      * submit findings and run the verified-authority deliberation server-side,
      * stream Band's audit trail back into our AuditLog (or read it from Band).
    Until then this raises so nobody silently ships against an empty backend.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or config.BAND_API_KEY
        self.base_url = base_url
        self.audit = AuditLog()

    def deliberate(self, **kwargs) -> DeliberationResult:
        raise NotImplementedError(
            "RemoteBandRoom.deliberate is not implemented — use LocalBandRoom. "
            "Wire a real Band API/SDK here once access is confirmed. See "
            "TODO(governance-owner)."
        )

    def escalation_gate(self, assessment: RiskAssessment) -> GateDecision:
        raise NotImplementedError(
            "RemoteBandRoom.escalation_gate is not implemented — use LocalBandRoom."
        )


def get_band_room(audit: AuditLog | None = None, authorizer: Authorizer | None = None):
    """Factory: return the configured Band backend.

    Defaults to LocalBandRoom. Set ``BAND_BACKEND=remote`` (with BAND_API_KEY)
    once the real Band integration is wired.
    """
    import os

    if os.getenv("BAND_BACKEND", "local").lower() == "remote":
        return RemoteBandRoom()
    return LocalBandRoom(audit=audit, authorizer=authorizer)
