"""SQLite adapter for the persistence ports.

Chosen for Core 0.1 because it needs no server process, which keeps the
local-first promise (Principle 3) true even on a machine with nothing else
installed, and keeps the test suite hermetic. The schema deliberately uses the
`indexed columns + JSON document` shape so the PostgreSQL + pgvector adapter
named in Blueprint 4.3 is a drop-in sibling rather than a redesign.

stdlib `sqlite3` is synchronous, so every call hops to a worker thread. A single
connection guarded by one lock is plenty for a single-owner Core and avoids
SQLite's cross-thread pitfalls entirely.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from jarvis.events.envelope import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    document       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_type        ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp   ON events(timestamp);

CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    document   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state);

CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash TEXT NOT NULL UNIQUE,
    prev_hash  TEXT,
    timestamp  TEXT NOT NULL,
    document   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key      TEXT PRIMARY KEY,
    document TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id         TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    subject           TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    project_scope     TEXT,
    confidence        REAL NOT NULL,
    sensitivity       TEXT NOT NULL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    expires_at        TEXT,
    last_confirmed_at TEXT NOT NULL,
    document          TEXT NOT NULL
);
-- One belief per (subject, predicate, scope): a new sighting must find the
-- existing entry rather than creating a rival copy of the same claim.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key
    ON memories(subject, predicate, IFNULL(project_scope, ''));
CREATE INDEX IF NOT EXISTS idx_memories_type    ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_scope   ON memories(project_scope);
CREATE INDEX IF NOT EXISTS idx_memories_recent  ON memories(last_confirmed_at);
"""


class SqliteStore:
    """Implements `jarvis.persistence.ports.Store`."""

    def __init__(self, path: str | Path = "jarvis.db") -> None:
        self._path = str(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        def _connect() -> sqlite3.Connection:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL keeps readers (HUD/debug dashboard) from blocking writers.
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_connect)

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await asyncio.to_thread(conn.close)

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open; call await store.open() first")
        return self._conn

    async def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        conn = self._require()
        async with self._lock:

            def _run() -> None:
                conn.execute(sql, params)
                conn.commit()

            await asyncio.to_thread(_run)

    async def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._require()
        async with self._lock:
            return await asyncio.to_thread(lambda: conn.execute(sql, params).fetchall())

    # -- EventStore ---------------------------------------------------------

    async def append_event(self, event: Event) -> None:
        await self._write(
            "INSERT OR IGNORE INTO events(event_id, type, timestamp, correlation_id, document) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.type,
                event.timestamp.isoformat(),
                event.correlation_id,
                event.to_json(),
            ),
        )

    async def list_events(
        self,
        *,
        correlation_id: str | None = None,
        type_prefix: str | None = None,
        limit: int = 200,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if type_prefix is not None:
            clauses.append("type LIKE ?")
            params.append(f"{type_prefix}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._read(
            f"SELECT document FROM events {where} ORDER BY timestamp ASC, rowid ASC LIMIT ?",
            tuple(params),
        )
        return [Event.from_dict(json.loads(r["document"])) for r in rows]

    # -- MissionStore -------------------------------------------------------

    async def save_mission(self, mission_id: str, state: str, record: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO missions(mission_id, state, updated_at, document) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mission_id) DO UPDATE SET "
            "state=excluded.state, updated_at=excluded.updated_at, document=excluded.document",
            (
                mission_id,
                state,
                str(record.get("updated_at", "")),
                json.dumps(record, sort_keys=True, default=str),
            ),
        )

    async def load_mission(self, mission_id: str) -> dict[str, Any] | None:
        rows = await self._read("SELECT document FROM missions WHERE mission_id = ?", (mission_id,))
        return json.loads(rows[0]["document"]) if rows else None

    async def list_missions(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if state is not None:
            rows = await self._read(
                "SELECT document FROM missions WHERE state = ? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            )
        else:
            rows = await self._read(
                "SELECT document FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return [json.loads(r["document"]) for r in rows]

    # -- AuditStore ---------------------------------------------------------

    async def append_audit(self, entry: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO audit(entry_hash, prev_hash, timestamp, document) VALUES (?, ?, ?, ?)",
            (
                entry["entry_hash"],
                entry.get("prev_hash"),
                entry["timestamp"],
                json.dumps(entry, sort_keys=True, default=str),
            ),
        )

    async def list_audit(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._read("SELECT document FROM audit ORDER BY seq ASC LIMIT ?", (limit,))
        return [json.loads(r["document"]) for r in rows]

    async def last_audit_hash(self) -> str | None:
        rows = await self._read("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1")
        return rows[0]["entry_hash"] if rows else None

    # -- StateStore ---------------------------------------------------------

    async def put_state(self, key: str, value: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO state(key, document) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET document=excluded.document",
            (key, json.dumps(value, sort_keys=True, default=str)),
        )

    async def get_state(self, key: str) -> dict[str, Any] | None:
        rows = await self._read("SELECT document FROM state WHERE key = ?", (key,))
        return json.loads(rows[0]["document"]) if rows else None

    # -- MemoryStore --------------------------------------------------------

    async def put_memory(self, record: dict[str, Any]) -> None:
        await self._write(
            "INSERT INTO memories(memory_id, type, subject, predicate, project_scope, "
            "confidence, sensitivity, pinned, expires_at, last_confirmed_at, document) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET "
            "type=excluded.type, subject=excluded.subject, predicate=excluded.predicate, "
            "project_scope=excluded.project_scope, confidence=excluded.confidence, "
            "sensitivity=excluded.sensitivity, pinned=excluded.pinned, "
            "expires_at=excluded.expires_at, last_confirmed_at=excluded.last_confirmed_at, "
            "document=excluded.document",
            (
                record["memory_id"],
                record["type"],
                record["subject"],
                record["predicate"],
                record.get("project_scope"),
                record["confidence"],
                record["sensitivity"],
                1 if record.get("pinned") else 0,
                record.get("expires_at"),
                record["last_confirmed_at"],
                json.dumps(record, sort_keys=True, default=str),
            ),
        )

    async def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        rows = await self._read("SELECT document FROM memories WHERE memory_id = ?", (memory_id,))
        return json.loads(rows[0]["document"]) if rows else None

    async def find_memory(
        self, subject: str, predicate: str, project_scope: str | None
    ) -> dict[str, Any] | None:
        rows = await self._read(
            "SELECT document FROM memories WHERE subject = ? AND predicate = ? "
            "AND IFNULL(project_scope, '') = ?",
            (subject, predicate, project_scope or ""),
        )
        return json.loads(rows[0]["document"]) if rows else None

    async def list_memories(
        self,
        *,
        type: str | None = None,
        subject: str | None = None,
        project_scope: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        if project_scope is not None:
            clauses.append("IFNULL(project_scope, '') = ?")
            params.append(project_scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._read(
            f"SELECT document FROM memories {where} "
            "ORDER BY confidence DESC, last_confirmed_at DESC LIMIT ?",
            tuple(params),
        )
        return [json.loads(r["document"]) for r in rows]

    async def delete_memories(self, memory_ids: list[str]) -> list[str]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        # Read back what actually exists first, so the caller can audit the
        # true set of removals rather than what it hoped to remove.
        rows = await self._read(
            f"SELECT memory_id FROM memories WHERE memory_id IN ({placeholders})",
            tuple(memory_ids),
        )
        existing = [r["memory_id"] for r in rows]
        if existing:
            await self._write(
                f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                tuple(memory_ids),
            )
        return existing

    async def list_memories_since(
        self, since_iso: str, *, include_pinned: bool = False
    ) -> list[dict[str, Any]]:
        pinned_clause = "" if include_pinned else "AND pinned = 0"
        rows = await self._read(
            f"SELECT document FROM memories WHERE last_confirmed_at >= ? {pinned_clause} "
            "ORDER BY last_confirmed_at ASC",
            (since_iso,),
        )
        return [json.loads(r["document"]) for r in rows]
