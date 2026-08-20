"""Control surface "What JARVIS Knows" - Blueprint 8.4.

    * Liste aller gespeicherten Präferenzen, Routinen und Projekthypothesen.
    * Quelle, Confidence, letzte Bestätigung und betroffene Automationen sichtbar.
    * Actions: Correct, Forget, Pin, Don't Learn This, Make Temporary.
    * Privacy Mode: kein Verhalten lernen, optional kein Gesprächs-Memory,
      Screen/Kamera-Learning aus.
    * "Jarvis, vergiss die letzten 30 Minuten" erzeugt einen nachvollziehbaren
      Delete-Workflow.

One subtlety governs the whole module. "Nachvollziehbar" and "vergiss" pull in
opposite directions: an audit trail that records what was deleted would keep
the deleted content alive in the audit log, which is precisely what the owner
asked to be rid of - and the audit log is hash-chained and append-only, so it
is the one place the content could never be removed from afterwards. So every
delete here audits *identity and shape* - ids, types, counts, the time window -
and never values. The owner can prove what happened without the record
re-leaking what was forgotten.

`Pin` protects an entry from bulk deletion. Blueprint 8.4 lists Pin and the
forget-window as separate actions; pinning something and then having a blanket
"forget the last 30 minutes" silently remove it anyway would make Pin
meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, new_id, utc_now
from jarvis.memory.learning import ProposalStatus
from jarvis.memory.models import MemoryEntry, MemoryType
from jarvis.memory.service import MemoryService

#: Default for "Make Temporary" when the caller does not say how long.
DEFAULT_TEMPORARY_TTL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DeleteReceipt:
    """Proof that a deletion happened, carrying no deleted content."""

    deleted_ids: tuple[str, ...]
    types: dict[str, int]
    reason: str
    window_minutes: int | None = None
    protected_pinned: int = 0

    @property
    def count(self) -> int:
        return len(self.deleted_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deleted_count": self.count,
            "deleted_ids": list(self.deleted_ids),
            "types": self.types,
            "reason": self.reason,
            "window_minutes": self.window_minutes,
            "protected_pinned": self.protected_pinned,
        }


class MemoryControl:
    """The operations behind "What JARVIS Knows"."""

    def __init__(
        self,
        memory: MemoryService,
        *,
        audit: AuditLogger | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._memory = memory
        self._audit = audit
        self._bus = bus

    # -- the list -----------------------------------------------------------

    async def what_jarvis_knows(
        self, *, type: MemoryType | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Every stored belief, with provenance and its affected automations."""
        entries = await self._memory.entries(type=type, limit=limit)
        proposals = self._memory.proposals()

        rows: list[dict[str, Any]] = []
        for entry in entries:
            affected = [p.to_dict() for p in proposals if p.source_memory_id == entry.memory_id]
            rows.append({**entry.to_dict(), "affected_automations": affected})
        return rows

    # -- the five actions ---------------------------------------------------

    async def correct(self, memory_id: str, value: Any) -> MemoryEntry | None:
        """Blueprint 8.4 "Correct". A correction is the strongest signal there is."""
        entry = await self._memory.get(memory_id)
        if entry is None:
            return None
        corrected = entry.corrected_to(value)
        await self._memory.put(corrected)
        await self._log(
            action="memory.correct",
            subject=f"{entry.subject}:{entry.predicate}",
            decision="corrected",
            memory_id=memory_id,
            confidence=corrected.confidence,
        )
        return corrected

    async def forget(self, memory_id: str, *, reason: str = "owner requested") -> DeleteReceipt:
        """Blueprint 8.4 "Forget". Removes one entry."""
        entry = await self._memory.get(memory_id)
        types = {str(entry.type): 1} if entry else {}
        deleted = await self._memory._store.delete_memories([memory_id])
        receipt = DeleteReceipt(deleted_ids=tuple(deleted), types=types, reason=reason)
        await self._audit_delete(receipt)
        await self._announce_delete(receipt)
        return receipt

    async def pin(self, memory_id: str) -> MemoryEntry | None:
        """Blueprint 8.4 "Pin". Protects from decay and from forget windows."""
        entry = await self._memory.get(memory_id)
        if entry is None:
            return None
        pinned = entry.pinned_copy()
        await self._memory.put(pinned)
        await self._log(
            action="memory.pin",
            subject=f"{entry.subject}:{entry.predicate}",
            decision="pinned",
            memory_id=memory_id,
        )
        return pinned

    async def dont_learn_this(
        self, subject: str, predicate: str = "*", *, forget_existing: bool = True
    ) -> DeleteReceipt:
        """Blueprint 8.4 "Don't Learn This".

        Blocks the pattern going forward and, by default, removes what is
        already stored for it. Telling a system to stop learning something it
        has already learned should not leave the old copy behind.
        """
        self._memory.privacy.settings.block(subject, predicate)
        await self._memory.save_privacy()

        receipt = DeleteReceipt(deleted_ids=(), types={}, reason="don't learn this")
        if forget_existing:
            entries = [
                e
                for e in await self._memory.entries()
                if e.subject == subject and (predicate == "*" or e.predicate == predicate)
            ]
            receipt = await self._delete_entries(entries, reason="don't learn this")

        await self._log(
            action="memory.dont_learn_this",
            subject=f"{subject}:{predicate}",
            decision="blocked",
            forgot=receipt.count,
        )
        return receipt

    async def make_temporary(
        self, memory_id: str, ttl: timedelta = DEFAULT_TEMPORARY_TTL
    ) -> MemoryEntry | None:
        """Blueprint 8.4 "Make Temporary"."""
        entry = await self._memory.get(memory_id)
        if entry is None:
            return None
        temporary = entry.made_temporary(ttl)
        await self._memory.put(temporary)
        await self._log(
            action="memory.make_temporary",
            subject=f"{entry.subject}:{entry.predicate}",
            decision="temporary",
            memory_id=memory_id,
            expires_at=temporary.expires_at.isoformat() if temporary.expires_at else None,
        )
        return temporary

    # -- "Jarvis, vergiss die letzten 30 Minuten" ---------------------------

    async def forget_window(self, minutes: int = 30) -> DeleteReceipt:
        """The delete workflow from Blueprint 8.4, made traceable.

        Everything confirmed inside the window goes, except pinned entries.
        The receipt names how many were protected, so the outcome is never
        silently different from what was asked for.
        """
        cutoff = utc_now() - timedelta(minutes=minutes)
        candidates = await self._memory._store.list_memories_since(
            cutoff.isoformat(), include_pinned=True
        )
        entries = [MemoryEntry.from_dict(r) for r in candidates]
        deletable = [e for e in entries if not e.pinned]
        protected = len(entries) - len(deletable)

        receipt = await self._delete_entries(
            deletable,
            reason=f"owner asked to forget the last {minutes} minutes",
            window_minutes=minutes,
            protected_pinned=protected,
        )
        return receipt

    # -- privacy mode -------------------------------------------------------

    async def set_privacy(
        self,
        *,
        learn_behaviour: bool | None = None,
        conversation_memory: bool | None = None,
        screen_camera_learning: bool | None = None,
    ) -> dict[str, Any]:
        settings = self._memory.privacy.settings
        if learn_behaviour is not None:
            settings.learn_behaviour = learn_behaviour
        if conversation_memory is not None:
            settings.conversation_memory = conversation_memory
        if screen_camera_learning is not None:
            settings.screen_camera_learning = screen_camera_learning
        await self._memory.save_privacy()

        await self._log(
            action="memory.privacy_mode",
            subject="privacy",
            decision="updated",
            **settings.to_dict(),
        )
        return settings.to_dict()

    # -- routine proposals --------------------------------------------------

    async def pending_routines(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._memory.proposals(status=ProposalStatus.PROPOSED)]

    async def decide_routine(self, proposal_id: str, *, approve: bool) -> dict[str, Any] | None:
        decided = await self._memory.decide_proposal(proposal_id, approve=approve)
        return decided.to_dict() if decided else None

    # -- shared plumbing ----------------------------------------------------

    async def _delete_entries(
        self,
        entries: list[MemoryEntry],
        *,
        reason: str,
        window_minutes: int | None = None,
        protected_pinned: int = 0,
    ) -> DeleteReceipt:
        types: dict[str, int] = {}
        for entry in entries:
            types[str(entry.type)] = types.get(str(entry.type), 0) + 1

        deleted = await self._memory._store.delete_memories([e.memory_id for e in entries])
        receipt = DeleteReceipt(
            deleted_ids=tuple(deleted),
            types=types,
            reason=reason,
            window_minutes=window_minutes,
            protected_pinned=protected_pinned,
        )
        await self._audit_delete(receipt)
        await self._announce_delete(receipt)
        return receipt

    async def _audit_delete(self, receipt: DeleteReceipt) -> None:
        """Audit the deletion without writing the deleted content anywhere.

        `prev_state` deliberately carries counts, not values: the audit log is
        append-only and hash-chained, so anything recorded here could never be
        forgotten afterwards.
        """
        if self._audit is None:
            return
        await self._audit.log(
            action="memory.delete",
            actor="owner",
            subject="memory",
            decision="deleted",
            correlation_id=new_id(),
            prev_state={"deleted_count": receipt.count, "types": receipt.types},
            reason=receipt.reason,
            deleted_ids=list(receipt.deleted_ids),
            window_minutes=receipt.window_minutes,
            protected_pinned=receipt.protected_pinned,
        )

    async def _announce_delete(self, receipt: DeleteReceipt) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            Event(
                type=ev.MEMORY_DELETED,
                source="memory-control",
                priority=Priority.URGENT,
                payload=receipt.to_dict(),
            )
        )

    async def _log(self, *, action: str, subject: str, decision: str, **details: Any) -> None:
        if self._audit is None:
            return
        await self._audit.log(
            action=action,
            actor="owner",
            subject=subject,
            decision=decision,
            correlation_id=new_id(),
            **details,
        )
