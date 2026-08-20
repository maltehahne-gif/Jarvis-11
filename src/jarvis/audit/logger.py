"""Audit Logger - Blueprint 5.1 and 7.2.

"Nachvollziehbare, manipulationsgeschützte Historie kritischer Aktionen" and
"Jeder kritische State Change bekommt Audit Event, vorherigen Zustand und
möglichst Rollback-Punkt."

Tamper evidence comes from a hash chain: each entry commits to its predecessor,
so editing or deleting any past entry invalidates every hash after it. That is
detection, not prevention - which is the honest guarantee for a log that lives
on the owner's own disk. Append-only storage plus off-box replication is the
Phase-later hardening; the chain is what makes such tampering *visible*.

The audit log is deliberately separate from the event log. Events are the
system's nervous system and may be filtered, dropped or expired; audit entries
are the record of consequential decisions and are never dropped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.events.envelope import new_id, utc_now
from jarvis.persistence.ports import AuditStore

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One consequential decision or action, committed to the chain."""

    action: str
    actor: str
    subject: str
    decision: str
    correlation_id: str
    prev_state: dict[str, Any] | None = None
    rollback_point: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    entry_id: str = field(default_factory=new_id)
    timestamp: datetime = field(default_factory=utc_now)

    def body(self) -> dict[str, Any]:
        """The canonical, hashed content of the entry."""
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "actor": self.actor,
            "subject": self.subject,
            "decision": self.decision,
            "correlation_id": self.correlation_id,
            "prev_state": self.prev_state,
            "rollback_point": self.rollback_point,
            "details": self.details,
        }


def compute_hash(body: dict[str, Any], prev_hash: str) -> str:
    """sha256 over `prev_hash` plus the canonical JSON body.

    `sort_keys` + fixed separators make the encoding canonical, so the same
    logical entry always hashes identically across processes and Python
    versions.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


class AuditLogger:
    """Appends hash-chained entries to an `AuditStore`."""

    def __init__(self, store: AuditStore) -> None:
        self._store = store
        self._tip: str | None = None

    async def _current_tip(self) -> str:
        if self._tip is None:
            self._tip = await self._store.last_audit_hash() or GENESIS
        return self._tip

    async def record(self, entry: AuditEntry) -> dict[str, Any]:
        """Commit one entry. Returns the stored record including its hashes."""
        prev_hash = await self._current_tip()
        body = entry.body()
        entry_hash = compute_hash(body, prev_hash)
        record = {**body, "prev_hash": prev_hash, "entry_hash": entry_hash}
        await self._store.append_audit(record)
        self._tip = entry_hash
        return record

    async def log(
        self,
        *,
        action: str,
        actor: str,
        subject: str,
        decision: str,
        correlation_id: str,
        prev_state: dict[str, Any] | None = None,
        rollback_point: dict[str, Any] | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        """Convenience wrapper over `record` for call sites."""
        return await self.record(
            AuditEntry(
                action=action,
                actor=actor,
                subject=subject,
                decision=decision,
                correlation_id=correlation_id,
                prev_state=prev_state,
                rollback_point=rollback_point,
                details=details,
            )
        )

    async def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        return await self._store.list_audit(limit=limit)

    async def verify_chain(self, limit: int = 10_000) -> tuple[bool, str | None]:
        """Recompute the chain. Returns `(ok, first_broken_entry_id)`."""
        prev_hash = GENESIS
        for record in await self._store.list_audit(limit=limit):
            body = {k: v for k, v in record.items() if k not in ("prev_hash", "entry_hash")}
            if record.get("prev_hash") != prev_hash:
                return False, record.get("entry_id")
            if compute_hash(body, prev_hash) != record.get("entry_hash"):
                return False, record.get("entry_id")
            prev_hash = record["entry_hash"]
        return True, None
