"""Persistence ports.

The Core talks to storage only through these protocols. Blueprint 4.3 names
PostgreSQL + pgvector as the target store; keeping the Core behind a port is
what makes that swap a one-adapter change rather than a rewrite, and it is the
same "stable interfaces inside a modular monolith" rule from Blueprint 4.2.

Records cross this boundary as plain dicts with a few indexed columns lifted
out. That shape maps 1:1 onto a `jsonb` column plus indexes in PostgreSQL.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from jarvis.events.envelope import Event


@runtime_checkable
class EventStore(Protocol):
    """Durable, append-only event log."""

    async def append_event(self, event: Event) -> None: ...

    async def list_events(
        self,
        *,
        correlation_id: str | None = None,
        type_prefix: str | None = None,
        limit: int = 200,
    ) -> list[Event]: ...


@runtime_checkable
class MissionStore(Protocol):
    """Mission persistence. Missions must survive a process restart (DoD 5.4)."""

    async def save_mission(self, mission_id: str, state: str, record: dict[str, Any]) -> None: ...

    async def load_mission(self, mission_id: str) -> dict[str, Any] | None: ...

    async def list_missions(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class AuditStore(Protocol):
    """Append-only audit trail. Entries are hash-chained by the AuditLogger."""

    async def append_audit(self, entry: dict[str, Any]) -> None: ...

    async def list_audit(self, *, limit: int = 200) -> list[dict[str, Any]]: ...

    async def last_audit_hash(self) -> str | None: ...


@runtime_checkable
class StateStore(Protocol):
    """Small key/value slots for State Manager snapshots."""

    async def put_state(self, key: str, value: dict[str, Any]) -> None: ...

    async def get_state(self, key: str) -> dict[str, Any] | None: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Long-term memory - Blueprint 8.

    Entries are keyed by `memory_id` but *looked up* by their belief key
    (subject, predicate, project_scope), because a new sighting has to find
    the existing belief it confirms or contradicts before it can be folded in.

    `delete_memories` returns the ids it actually removed. The caller needs
    that to write a truthful audit entry for the delete workflow in 8.4 -
    without ever handing the deleted values back.
    """

    async def put_memory(self, record: dict[str, Any]) -> None: ...

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None: ...

    async def find_memory(
        self, subject: str, predicate: str, project_scope: str | None
    ) -> dict[str, Any] | None: ...

    async def list_memories(
        self,
        *,
        type: str | None = None,
        subject: str | None = None,
        project_scope: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...

    async def delete_memories(self, memory_ids: list[str]) -> list[str]: ...

    async def list_memories_since(
        self, since_iso: str, *, include_pinned: bool = False
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class Store(EventStore, MissionStore, AuditStore, StateStore, MemoryStore, Protocol):
    """The full storage surface the Core depends on."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...
