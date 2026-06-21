"""Append-only audit trail for the Band room.

Every consequential step in the governed room writes an :class:`AuditRecord` here.
The log is append-only by construction (no update/delete API) — that immutability
IS the audit guarantee.
"""

from __future__ import annotations

from datetime import datetime, timezone

from schemas import AuditRecord


class AuditLog:
    """In-memory append-only audit log.

    TODO(governance-owner): back this with the real Band audit trail (durable,
    tamper-evident) once RemoteBandRoom is wired. The append-only interface is
    intentionally minimal so the backend can swap without callers changing.
    """

    def __init__(self, *, echo: bool = False) -> None:
        self._records: list[AuditRecord] = []
        self._echo = echo

    def append(
        self,
        actor: str,
        action: str,
        *,
        authority_granted: bool = False,
        payload: dict | None = None,
    ) -> AuditRecord:
        """Append one audit record and return it."""
        record = AuditRecord(
            actor=actor,
            action=action,
            authority_granted=authority_granted,
            timestamp=datetime.now(timezone.utc),
            payload=payload or {},
        )
        self._records.append(record)
        if self._echo:
            print(
                f"  [audit] {record.timestamp.isoformat()} "
                f"{record.actor} :: {record.action} "
                f"(authority={'yes' if record.authority_granted else 'no'})"
            )
        return record

    @property
    def records(self) -> list[AuditRecord]:
        """A copy of the trail (read-only view; the log stays append-only)."""
        return list(self._records)

    def as_dicts(self) -> list[dict]:
        """Serialize the trail (for the web UI / reasoning log)."""
        return [r.model_dump(mode="json") for r in self._records]

    def __len__(self) -> int:
        return len(self._records)
