"""Capability Registry - Blueprint 5.1.

The single catalogue of everything JARVIS can do. Nothing executes that is not
registered here, which is what gives the Permission Engine a closed world to
reason about: an action with no entry has no declared risk level, so it is
denied rather than guessed at.
"""

from __future__ import annotations

from jarvis.capability.models import Capability, Health
from jarvis.permission.levels import PermissionLevel


class UnknownCapability(KeyError):
    """Raised when an action has no registry entry."""


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, capability: Capability, *, replace: bool = False) -> Capability:
        if capability.name in self._caps and not replace:
            raise ValueError(f"capability already registered: {capability.name}")
        self._caps[capability.name] = capability
        return capability

    def get(self, name: str) -> Capability:
        try:
            return self._caps[name]
        except KeyError as exc:
            raise UnknownCapability(name) from exc

    def has(self, name: str) -> bool:
        return name in self._caps

    def names(self) -> list[str]:
        return sorted(self._caps)

    def all(self) -> list[Capability]:
        return [self._caps[n] for n in self.names()]

    def set_health(self, name: str, health: Health) -> Capability:
        """Health is mutable state on an otherwise frozen record."""
        from dataclasses import replace as _replace

        updated = _replace(self.get(name), health=health)
        self._caps[name] = updated
        return updated

    def by_level(self, level: PermissionLevel) -> list[Capability]:
        return [c for c in self.all() if c.level is level]

    def to_dict(self) -> list[dict[str, object]]:
        return [c.to_dict() for c in self.all()]
