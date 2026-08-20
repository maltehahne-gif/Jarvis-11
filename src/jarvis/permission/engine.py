"""Permission Engine - Blueprint 5.1 ("Vor jeder Aktion deterministische Policy
prüfen") and 7.1/7.2.

`check()` is deliberately synchronous and side-effect free. A permission
decision must be reproducible from its inputs alone: same capability, same
params, same context, same grants, same verdict - every time, with no network
call and no model in the loop. Emitting events and audit entries is the
Execution Gateway's job, not the engine's.

Evaluation order is itself a security property and runs strictest-first:

1. kill switch          - a stopped system executes nothing
2. P6 Forbidden         - "nie ausführen", not negotiable by any override
3. gates                - deterministic pattern blocks
4. grant coverage       - mission-scoped, expiring rights
5. named scopes         - capability's `required_grants`
6. level table          - the P0-P6 default from Blueprint 7.1
7. standing approval    - a matching, unexpired confirmation upgrades to ALLOW

Nothing later in the list can loosen anything earlier in it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jarvis.capability.models import Capability, ExecutionContext
from jarvis.events.envelope import new_id, utc_now
from jarvis.permission.levels import PermissionLevel
from jarvis.permission.policy import (
    CapabilityGrant,
    Confirmation,
    Decision,
    Policy,
    stricter,
)


def fingerprint(capability_name: str, params: dict[str, Any], mission_id: str | None) -> str:
    """Stable identity of one concrete action request.

    Approvals are bound to this value, so consent for "send mail to Anna" can
    never be replayed as consent for "send mail to everyone": different params,
    different fingerprint, no standing approval.
    """
    canonical = json.dumps(
        {"capability": capability_name, "params": params, "mission_id": mission_id},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Approval:
    """A confirmation the owner actually gave, for one fingerprint."""

    fingerprint: str
    confirmation: Confirmation
    expires_at: datetime
    device_id: str | None = None
    approval_id: str = field(default_factory=new_id)
    granted_at: datetime = field(default_factory=utc_now)

    def is_valid(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) < self.expires_at

    def satisfies(self, required: Confirmation) -> bool:
        order = {Confirmation.NONE: 0, Confirmation.SIMPLE: 1, Confirmation.STRONG: 2}
        return order[self.confirmation] >= order[required]


@dataclass(frozen=True, slots=True)
class Verdict:
    """The engine's answer, with enough detail to audit and to explain."""

    decision: Decision
    reason: str
    rule: str
    level: PermissionLevel
    capability: str
    fingerprint: str
    confirmation: Confirmation = Confirmation.NONE

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reason": self.reason,
            "rule": self.rule,
            "level": self.level.code,
            "capability": self.capability,
            "fingerprint": self.fingerprint,
            "confirmation": str(self.confirmation),
        }


class ApprovalLedger:
    """Pending confirmation requests and the approvals that resolve them."""

    def __init__(self) -> None:
        self._approvals: dict[str, Approval] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def request(
        self,
        *,
        fp: str,
        capability: str,
        params: dict[str, Any],
        confirmation: Confirmation,
        mission_id: str | None,
    ) -> dict[str, Any]:
        record = {
            "fingerprint": fp,
            "capability": capability,
            "params": params,
            "confirmation": str(confirmation),
            "mission_id": mission_id,
            "requested_at": utc_now().isoformat(),
        }
        self._pending[fp] = record
        return record

    def pending(self) -> list[dict[str, Any]]:
        return list(self._pending.values())

    def grant(
        self,
        fp: str,
        *,
        confirmation: Confirmation = Confirmation.SIMPLE,
        device_id: str | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> Approval:
        approval = Approval(
            fingerprint=fp,
            confirmation=confirmation,
            device_id=device_id,
            expires_at=utc_now() + ttl,
        )
        self._approvals[fp] = approval
        self._pending.pop(fp, None)
        return approval

    def deny(self, fp: str) -> None:
        self._pending.pop(fp, None)
        self._approvals.pop(fp, None)

    def find(self, fp: str) -> Approval | None:
        approval = self._approvals.get(fp)
        if approval is None:
            return None
        if not approval.is_valid():
            del self._approvals[fp]
            return None
        return approval

    def consume(self, fp: str) -> None:
        """Approvals are single-use; a second call must be confirmed again."""
        self._approvals.pop(fp, None)


class PermissionEngine:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy.default()
        self.approvals = ApprovalLedger()
        self._grants: dict[str, CapabilityGrant] = {}
        self._kill_switch_engaged = False
        self._kill_switch_reason = ""

    # -- kill switch (Blueprint 7.2) ----------------------------------------

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    @property
    def kill_switch_reason(self) -> str:
        return self._kill_switch_reason

    def engage_kill_switch(self, reason: str = "owner requested stop") -> None:
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason

    def release_kill_switch(self) -> None:
        self._kill_switch_engaged = False
        self._kill_switch_reason = ""

    # -- mission-scoped grants (Blueprint 7.2) ------------------------------

    def issue_grant(
        self,
        mission_id: str,
        capabilities: frozenset[str],
        *,
        grants: frozenset[str] = frozenset(),
        ttl: timedelta | None = None,
        device_id: str | None = None,
    ) -> CapabilityGrant:
        grant = CapabilityGrant(
            mission_id=mission_id,
            capabilities=capabilities,
            grants=grants,
            device_id=device_id,
            expires_at=utc_now() + (ttl or self.policy.default_grant_ttl),
        )
        self._grants[mission_id] = grant
        return grant

    def revoke_grant(self, mission_id: str) -> None:
        self._grants.pop(mission_id, None)

    def grant_for(self, mission_id: str) -> CapabilityGrant | None:
        return self._grants.get(mission_id)

    def expired_grants(self) -> list[CapabilityGrant]:
        return [g for g in self._grants.values() if g.is_expired()]

    # -- the decision -------------------------------------------------------

    def check(
        self,
        capability: Capability,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> Verdict:
        fp = fingerprint(capability.name, params, context.mission_id)

        def verdict(
            decision: Decision,
            rule: str,
            reason: str,
            confirmation: Confirmation = Confirmation.NONE,
        ) -> Verdict:
            return Verdict(
                decision=decision,
                reason=reason,
                rule=rule,
                level=capability.level,
                capability=capability.name,
                fingerprint=fp,
                confirmation=confirmation,
            )

        # 1. Kill switch. Nothing runs while the owner has stopped the system.
        if self._kill_switch_engaged:
            return verdict(
                Decision.DENY,
                "kill_switch",
                f"kill switch engaged: {self._kill_switch_reason}",
            )

        # 2. P6 Forbidden. Checked before overrides so it cannot be configured
        #    away, and before gates so the reason reported is the honest one.
        if capability.level is PermissionLevel.P6_FORBIDDEN:
            return verdict(
                Decision.DENY,
                "forbidden_level",
                f"{capability.name} is P6 (forbidden) and is never executed",
            )

        # 3. Deterministic gates.
        for gate in self.policy.gates:
            result = gate(capability, params, context)
            if result is not None:
                return verdict(Decision.DENY, result.rule, result.reason)

        # 4. Mission-scoped grant coverage.
        if context.mission_id is not None:
            grant = self._grants.get(context.mission_id)
            if grant is None:
                return verdict(
                    Decision.DENY,
                    "no_grant",
                    f"mission {context.mission_id} holds no capability grant",
                )
            if grant.is_expired():
                return verdict(
                    Decision.DENY,
                    "grant_expired",
                    f"capability grant for mission {context.mission_id} expired at "
                    f"{grant.expires_at.isoformat()}",
                )
            if not grant.covers(capability.name):
                return verdict(
                    Decision.DENY,
                    "grant_scope",
                    f"grant for mission {context.mission_id} does not cover {capability.name}",
                )

        # 5. Named scopes the capability declares it needs.
        held = set(context.grants)
        if context.mission_id is not None:
            mission_grant = self._grants.get(context.mission_id)
            if mission_grant is not None:
                held |= set(mission_grant.grants)
        missing = capability.required_grants - held
        if missing:
            return verdict(
                Decision.DENY,
                "missing_scope",
                f"missing required grant(s): {', '.join(sorted(missing))}",
            )

        # 6. The P0-P6 level table, tightened by any capability override.
        base = self.policy.decision_for(capability.level)
        override = self.policy.capability_overrides.get(capability.name)
        decision, confirmation = stricter(base, override) if override else base
        rule = "level_policy" if not override else "capability_override"

        if decision is Decision.DENY:
            return verdict(decision, rule, f"policy denies {capability.level.code} actions")

        if decision is Decision.REQUIRE_CONFIRMATION:
            # 7. A standing approval for this exact fingerprint clears the gate.
            approval = self.approvals.find(fp)
            if approval is not None and approval.satisfies(confirmation):
                return verdict(
                    Decision.ALLOW,
                    "approval_granted",
                    f"owner approved this action ({approval.confirmation}) at "
                    f"{approval.granted_at.isoformat()}",
                    confirmation,
                )
            return verdict(
                Decision.REQUIRE_CONFIRMATION,
                rule,
                f"{capability.level.code} requires {confirmation} confirmation",
                confirmation,
            )

        return verdict(Decision.ALLOW, rule, f"{capability.level.code} is allowed automatically")
