"""Permission policy and deterministic gates - Blueprint 7.1 and 7.2.

The policy is data, not prose: a table from risk level to decision, plus a list
of gates that can hard-deny regardless of level. Both are evaluated by code that
never sees a model's output, which is what "PreToolUse/Hook-ähnliche Gates
blockieren gefährliche Aufrufe deterministisch" requires.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.events.envelope import utc_now
from jarvis.permission.levels import PermissionLevel

if TYPE_CHECKING:
    from jarvis.capability.models import Capability, ExecutionContext


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


class Confirmation(StrEnum):
    """How strongly the owner must confirm before a gated action proceeds."""

    NONE = "none"
    #: A plain yes/no on any already-trusted surface.
    SIMPLE = "simple"
    #: Passkey or biometry on an unlocked, trusted device (Blueprint 7.2:
    #: "Voice-ID ist Komfortsignal, kein alleiniger Authenticator").
    STRONG = "strong"


#: Blueprint 7.1, column "Standard", expressed as a table.
DEFAULT_LEVEL_POLICY: dict[PermissionLevel, tuple[Decision, Confirmation]] = {
    PermissionLevel.P0_OBSERVE: (Decision.ALLOW, Confirmation.NONE),
    PermissionLevel.P1_SAFE: (Decision.ALLOW, Confirmation.NONE),
    PermissionLevel.P2_REVERSIBLE: (Decision.ALLOW, Confirmation.NONE),
    PermissionLevel.P3_SENSITIVE: (Decision.REQUIRE_CONFIRMATION, Confirmation.SIMPLE),
    PermissionLevel.P4_CRITICAL: (Decision.REQUIRE_CONFIRMATION, Confirmation.STRONG),
    PermissionLevel.P5_RESTRICTED: (Decision.REQUIRE_CONFIRMATION, Confirmation.STRONG),
    PermissionLevel.P6_FORBIDDEN: (Decision.DENY, Confirmation.NONE),
}


@dataclass(frozen=True, slots=True)
class GateResult:
    """A gate's veto. Gates may only deny; they can never widen permission."""

    rule: str
    reason: str


class Gate(Protocol):
    """A deterministic pre-execution check.

    Returns `None` to abstain, or a `GateResult` to block. Gates run before the
    level table so a forbidden pattern cannot be argued up by a policy override.
    """

    name: str

    def __call__(
        self, capability: Capability, params: dict[str, Any], context: ExecutionContext
    ) -> GateResult | None: ...


@dataclass(frozen=True, slots=True)
class DenyCapabilityGate:
    """Hard block-list of capability names. P6 by configuration."""

    blocked: frozenset[str]
    name: str = "deny_capability"

    def __call__(
        self, capability: Capability, params: dict[str, Any], context: ExecutionContext
    ) -> GateResult | None:
        if capability.name in self.blocked:
            return GateResult(self.name, f"capability {capability.name} is on the deny list")
        return None


@dataclass(frozen=True, slots=True)
class UnhealthyCapabilityGate:
    """Refuse to run a capability the registry reports as unavailable."""

    name: str = "unhealthy_capability"

    def __call__(
        self, capability: Capability, params: dict[str, Any], context: ExecutionContext
    ) -> GateResult | None:
        from jarvis.capability.models import Health

        if capability.health is Health.UNAVAILABLE:
            return GateResult(self.name, f"capability {capability.name} is unavailable")
        return None


@dataclass(frozen=True, slots=True)
class SecretsInParamsGate:
    """Block obvious credential material travelling through tool parameters.

    Blueprint 7.2: "das Modell soll Schlüssel möglichst nie im Klartext sehen"
    and 7.3: "keine secrets im prompt". Secrets belong in the credential broker;
    a parameter that looks like a key is a defect worth failing loudly on.
    """

    name: str = "secrets_in_params"
    markers: tuple[str, ...] = (
        "-----BEGIN",
        "sk-ant-",
        "AKIA",
        "ghp_",
        "xoxb-",
    )

    def __call__(
        self, capability: Capability, params: dict[str, Any], context: ExecutionContext
    ) -> GateResult | None:
        for key, value in params.items():
            if not isinstance(value, str):
                continue
            for marker in self.markers:
                if marker in value:
                    return GateResult(
                        self.name,
                        f"parameter {key} appears to contain credential material",
                    )
        return None


@dataclass(frozen=True, slots=True)
class DeviceBindingGate:
    """P5 (Restricted) is "oft device-bound" - enforce that literally.

    An action at or above `min_level` must originate from a device the owner
    has marked trusted. An unknown or absent device id is not a trusted device.
    """

    trusted_devices: frozenset[str]
    min_level: PermissionLevel = PermissionLevel.P5_RESTRICTED
    name: str = "device_binding"

    def __call__(
        self, capability: Capability, params: dict[str, Any], context: ExecutionContext
    ) -> GateResult | None:
        if capability.level < self.min_level:
            return None
        if context.device_id is None:
            return GateResult(
                self.name,
                f"{capability.level.code} requires a trusted device; request carries none",
            )
        if context.device_id not in self.trusted_devices:
            return GateResult(
                self.name,
                f"{capability.level.code} requires a trusted device; "
                f"{context.device_id} is not trusted",
            )
        return None


@dataclass(slots=True)
class CapabilityGrant:
    """Temporary rights handed to a mission.

    Blueprint 7.2: "Agenten bekommen pro Mission temporäre Capabilities; Rechte
    laufen ab." A grant is scoped to one mission, names the capabilities it
    covers, and expires on a wall clock.
    """

    mission_id: str
    capabilities: frozenset[str]
    expires_at: datetime
    grants: frozenset[str] = frozenset()
    device_id: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def covers(self, capability_name: str) -> bool:
        return "*" in self.capabilities or capability_name in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "capabilities": sorted(self.capabilities),
            "grants": sorted(self.grants),
            "expires_at": self.expires_at.isoformat(),
            "device_id": self.device_id,
        }


@dataclass(slots=True)
class Policy:
    """The complete, inspectable permission configuration."""

    level_policy: dict[PermissionLevel, tuple[Decision, Confirmation]] = field(
        default_factory=lambda: dict(DEFAULT_LEVEL_POLICY)
    )
    #: Per-capability tightening. Only ever consulted to make a decision
    #: stricter - see `PermissionEngine._apply_override`.
    capability_overrides: dict[str, tuple[Decision, Confirmation]] = field(default_factory=dict)
    gates: list[Gate] = field(default_factory=list)
    trusted_devices: frozenset[str] = frozenset()
    default_grant_ttl: timedelta = timedelta(minutes=15)

    @classmethod
    def default(
        cls,
        *,
        trusted_devices: frozenset[str] = frozenset(),
        blocked_capabilities: frozenset[str] = frozenset(),
    ) -> Policy:
        gates: list[Gate] = [
            UnhealthyCapabilityGate(),
            SecretsInParamsGate(),
            DeviceBindingGate(trusted_devices=trusted_devices),
        ]
        if blocked_capabilities:
            gates.insert(0, DenyCapabilityGate(blocked=blocked_capabilities))
        return cls(gates=gates, trusted_devices=trusted_devices)

    def decision_for(self, level: PermissionLevel) -> tuple[Decision, Confirmation]:
        return self.level_policy.get(level, (Decision.DENY, Confirmation.NONE))


#: Ordering used to compare strictness. Later entries are stricter.
_STRICTNESS: dict[Decision, int] = {
    Decision.ALLOW: 0,
    Decision.REQUIRE_CONFIRMATION: 1,
    Decision.DENY: 2,
}
_CONFIRMATION_STRICTNESS: dict[Confirmation, int] = {
    Confirmation.NONE: 0,
    Confirmation.SIMPLE: 1,
    Confirmation.STRONG: 2,
}


def stricter(
    a: tuple[Decision, Confirmation], b: tuple[Decision, Confirmation]
) -> tuple[Decision, Confirmation]:
    """Return whichever of the two verdicts is more restrictive.

    Used so overrides can only ever tighten. A configuration mistake should cost
    convenience, never safety.
    """
    if _STRICTNESS[a[0]] != _STRICTNESS[b[0]]:
        return a if _STRICTNESS[a[0]] > _STRICTNESS[b[0]] else b
    return a if _CONFIRMATION_STRICTNESS[a[1]] >= _CONFIRMATION_STRICTNESS[b[1]] else b


GateFactory = Callable[[], Gate]
