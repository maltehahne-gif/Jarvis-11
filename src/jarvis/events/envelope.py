"""Core Event Envelope - Blueprint 5.2.

Every state change in JARVIS travels as one of these. The envelope is the
contract between Core, HUD, Mobile, Logs, Memory and Automation (Blueprint 5.1,
Event Bus). Field names and value sets are taken verbatim from the blueprint;
do not rename them without updating the spec.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class Sensitivity(StrEnum):
    """How freely an event may be shown, routed and retained.

    Drives the Memory privacy filter (Blueprint 8) and the device/display
    routing decision (Blueprint 10.5): a `SECRET` event must never be pushed to
    a shared speaker or an untrusted display.
    """

    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"


class Priority(StrEnum):
    """Delivery urgency. Governs notification behaviour, not correctness."""

    BACKGROUND = "background"
    NORMAL = "normal"
    URGENT = "urgent"
    CRITICAL = "critical"


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable, typed record of something that happened.

    Events are facts, never requests: they are emitted *after* a decision or an
    action, and they are persisted before they are fanned out. The UI renders
    only persisted events - it never invents status (Blueprint 7.3, "UI täuscht
    Status vor").
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "core"
    correlation_id: str = field(default_factory=new_id)
    user_id: str = "local-owner"
    device_id: str | None = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    priority: Priority = Priority.NORMAL
    ttl: int | None = None
    event_id: str = field(default_factory=new_id)
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "correlation_id": self.correlation_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "sensitivity": str(self.sensitivity),
            "priority": str(self.priority),
            "payload": self.payload,
            "ttl": self.ttl,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            event_id=data["event_id"],
            type=data["type"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=data["source"],
            correlation_id=data["correlation_id"],
            user_id=data["user_id"],
            device_id=data.get("device_id"),
            sensitivity=Sensitivity(data["sensitivity"]),
            priority=Priority(data["priority"]),
            payload=data.get("payload", {}),
            ttl=data.get("ttl"),
        )

    def derive(self, type: str, **overrides: Any) -> Event:
        """Create a follow-up event that stays in the same correlation chain."""
        base: dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "sensitivity": self.sensitivity,
            "source": self.source,
        }
        base.update(overrides)
        return Event(type=type, **base)
