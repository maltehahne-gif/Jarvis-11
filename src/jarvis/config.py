"""Core configuration.

Kept as one explicit object rather than scattered environment lookups, so the
security-relevant settings - trusted devices, blocked capabilities, budgets -
are visible in a single place and easy to review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from jarvis.execution.budget import Budget


@dataclass(slots=True)
class CoreConfig:
    #: Local-first (Principle 3): state lives on the owner's machine.
    db_path: str = "data/jarvis.db"
    #: Loopback only. Blueprint 7.2: "Remote-Zugriff nie über offen ins
    #: Internet gestellte Admin-Ports"; remote access arrives later via a
    #: private mesh, not by widening this bind address.
    host: str = "127.0.0.1"
    port: int = 8765
    trusted_devices: frozenset[str] = frozenset()
    blocked_capabilities: frozenset[str] = frozenset()
    grant_ttl: timedelta = timedelta(minutes=15)
    budget: Budget = field(default_factory=Budget)
    max_agent_turns: int = 6
    #: "rules" (default, offline-safe) or "claude-agent-sdk". Blueprint 6.2's
    #: Billing/Auth question decides when the latter is actually used; until
    #: then the Core stays fully functional on the rule-based provider.
    provider: str = "rules"

    @classmethod
    def from_env(cls) -> CoreConfig:
        def _set(name: str) -> frozenset[str]:
            raw = os.getenv(name, "")
            return frozenset(p.strip() for p in raw.split(",") if p.strip())

        return cls(
            db_path=os.getenv("JARVIS_DB", "data/jarvis.db"),
            host=os.getenv("JARVIS_HOST", "127.0.0.1"),
            port=int(os.getenv("JARVIS_PORT", "8765")),
            trusted_devices=_set("JARVIS_TRUSTED_DEVICES"),
            blocked_capabilities=_set("JARVIS_BLOCKED_CAPABILITIES"),
            provider=os.getenv("JARVIS_PROVIDER", "rules"),
        )

    def data_dir(self) -> Path:
        return Path(self.db_path).parent

    @property
    def offline(self) -> bool:
        """True when no cloud provider is configured.

        Derived rather than configured separately: the rule-based provider is
        the local one (Blueprint 6.1's "Offline / private Basics" row), so
        running on it *is* running offline. Two independent switches could
        disagree, and the disagreement would route private data to a cloud
        that is not actually there.
        """
        return self.provider == "rules"
