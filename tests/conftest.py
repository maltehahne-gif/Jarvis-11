"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.persistence.sqlite_store import SqliteStore


@pytest.fixture
async def store() -> AsyncIterator[SqliteStore]:
    s = SqliteStore(":memory:")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def config(tmp_path) -> CoreConfig:
    return CoreConfig(
        db_path=str(tmp_path / "jarvis.db"),
        trusted_devices=frozenset({"desk-01"}),
    )


@pytest.fixture
async def core(config: CoreConfig) -> AsyncIterator[JarvisCore]:
    c = JarvisCore(config)
    await c.start()
    try:
        yield c
    finally:
        await c.stop()
