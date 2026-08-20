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
