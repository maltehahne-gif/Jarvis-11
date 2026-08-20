"""State Manager - Blueprint 5.1.

"Aktueller Benutzer-, Geräte-, Mission-, Gesprächs- und Präsenzzustand."

State is held in memory for speed and snapshotted to the store on change, so a
restart comes back with the device map and presence intact instead of a blank
system. It stays deliberately small in Core 0.1: presence routing across
displays and speakers is Blueprint 10.5 material and belongs to the phase that
actually has devices to route between.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.events.envelope import utc_now
from jarvis.persistence.ports import StateStore

STATE_KEY = "core.state"


@dataclass(slots=True)
class DeviceState:
    device_id: str
    kind: str = "unknown"
    online: bool = False
    trusted: bool = False
    last_seen: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "kind": self.kind,
            "online": self.online,
            "trusted": self.trusted,
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceState:
        return cls(
            device_id=data["device_id"],
            kind=data.get("kind", "unknown"),
            online=data.get("online", False),
            trusted=data.get("trusted", False),
            last_seen=datetime.fromisoformat(data["last_seen"]),
        )


class StateManager:
    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._devices: dict[str, DeviceState] = {}
        self._active_missions: set[str] = set()
        self._conversation: list[dict[str, Any]] = []
        self._presence: str | None = None

    async def load(self) -> None:
        record = await self._store.get_state(STATE_KEY)
        if record is None:
            return
        self._devices = {
            d["device_id"]: DeviceState.from_dict(d) for d in record.get("devices", [])
        }
        self._active_missions = set(record.get("active_missions", []))
        self._presence = record.get("presence")

    async def _save(self) -> None:
        await self._store.put_state(STATE_KEY, self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        return {
            "devices": [d.to_dict() for d in self._devices.values()],
            "active_missions": sorted(self._active_missions),
            "presence": self._presence,
            "conversation_turns": len(self._conversation),
            "updated_at": utc_now().isoformat(),
        }

    # -- devices ------------------------------------------------------------

    async def register_device(
        self, device_id: str, *, kind: str = "unknown", trusted: bool = False
    ) -> DeviceState:
        device = DeviceState(device_id=device_id, kind=kind, online=True, trusted=trusted)
        self._devices[device_id] = device
        await self._save()
        return device

    def device(self, device_id: str) -> DeviceState | None:
        return self._devices.get(device_id)

    def trusted_devices(self) -> frozenset[str]:
        return frozenset(d.device_id for d in self._devices.values() if d.trusted)

    # -- missions -----------------------------------------------------------

    async def mark_active(self, mission_id: str) -> None:
        self._active_missions.add(mission_id)
        await self._save()

    async def mark_inactive(self, mission_id: str) -> None:
        self._active_missions.discard(mission_id)
        await self._save()

    @property
    def active_missions(self) -> frozenset[str]:
        return frozenset(self._active_missions)

    # -- conversation -------------------------------------------------------

    def remember_turn(self, role: str, text: str, *, limit: int = 50) -> None:
        """Working memory only. Durable memory is Blueprint 8, a later phase."""
        self._conversation.append({"role": role, "text": text, "at": utc_now().isoformat()})
        del self._conversation[:-limit]

    def conversation(self) -> list[dict[str, Any]]:
        return list(self._conversation)

    # -- presence -----------------------------------------------------------

    async def set_presence(self, device_id: str | None) -> None:
        self._presence = device_id
        await self._save()

    @property
    def presence(self) -> str | None:
        return self._presence
