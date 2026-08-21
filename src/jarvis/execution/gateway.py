"""Execution Gateway - Blueprint 5.1.

"Tool-Aufrufe ausführen - nie direkter Modellzugriff auf OS/Secrets."

This module is the single chokepoint between intent and the real world. Agents,
routers and the API all produce *requests*; only the gateway turns a request
into an effect, and only after the full pipeline of Blueprint figure 2 has run:

    validate schema -> budget -> permission check -> execute -> verify -> audit

Every stage emits an event, and every consequential outcome is written to the
hash-chained audit log with the state that preceded it. That combination is
what satisfies "Jede Aktion erzeugt Events und Audit-Logs" (DoD 5.4) and
"Jeder kritische State Change bekommt Audit Event, vorherigen Zustand und
möglichst Rollback-Punkt" (Blueprint 7.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.capability.models import Capability, ExecutionContext, SchemaError
from jarvis.capability.registry import CapabilityRegistry, UnknownCapability
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, utc_now
from jarvis.execution.budget import BudgetTracker
from jarvis.permission.engine import PermissionEngine, Verdict
from jarvis.permission.levels import PermissionLevel
from jarvis.permission.policy import Decision
from jarvis.verify.verifier import VerificationReport, VerificationStatus, Verifier

log = logging.getLogger(__name__)

#: At or above this level, an action is "kritisch" for audit purposes and is
#: always written to the chain, allowed or not. Below it, denials are still
#: recorded - a blocked P1 is more interesting than a permitted one.
AUDIT_THRESHOLD = PermissionLevel.P2_REVERSIBLE


class ExecutionOutcome(StrEnum):
    EXECUTED = "executed"
    DENIED = "denied"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    FAILED = "failed"
    #: Unknown capability or parameters that do not match the declared schema.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    capability: str
    detail: str = ""
    verdict: Verdict | None = None
    result: dict[str, Any] = field(default_factory=dict)
    verification: VerificationReport | None = None
    duration_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        """True only when the tool ran *and* verification did not contradict it.

        An `UNVERIFIABLE` capability is allowed through here because no contract
        exists to disprove it; a `FAILED` or `ERRORED` verification is not.
        """
        if self.outcome is not ExecutionOutcome.EXECUTED:
            return False
        if self.verification is None:
            return True
        return self.verification.status in (
            VerificationStatus.PASSED,
            VerificationStatus.UNVERIFIABLE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "capability": self.capability,
            "detail": self.detail,
            "succeeded": self.succeeded,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "result": self.result,
            "verification": self.verification.to_dict() if self.verification else None,
            "duration_ms": round(self.duration_ms, 2),
        }


class ExecutionGateway:
    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        permissions: PermissionEngine,
        bus: EventBus,
        audit: AuditLogger,
        verifier: Verifier | None = None,
        budgets: BudgetTracker | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._bus = bus
        self._audit = audit
        self._verifier = verifier or Verifier()
        self._budgets = budgets or BudgetTracker()

    @property
    def budgets(self) -> BudgetTracker:
        return self._budgets

    async def execute(
        self,
        capability_name: str,
        params: dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        started = utc_now()

        # --- resolve ------------------------------------------------------
        try:
            capability = self._registry.get(capability_name)
        except UnknownCapability:
            return await self._reject(
                capability_name,
                params,
                context,
                f"no capability registered as {capability_name!r}",
            )

        # --- validate parameters before anything else touches them --------
        try:
            clean_params = capability.schema.validate(params)
        except SchemaError as exc:
            return await self._reject(capability_name, params, context, str(exc))

        # --- budget (checked before permission so a runaway loop cannot
        #     keep asking the owner for confirmations) ----------------------
        exhausted = self._budgets.exceeded(context.mission_id)
        if exhausted is not None:
            await self._bus.publish(
                Event(
                    type=ev.BUDGET_EXCEEDED,
                    source="execution-gateway",
                    correlation_id=context.correlation_id,
                    device_id=context.device_id,
                    priority=Priority.URGENT,
                    payload={
                        "capability": capability_name,
                        "mission_id": context.mission_id,
                        "limit": exhausted,
                    },
                )
            )
            return await self._deny_result(
                capability,
                clean_params,
                context,
                detail=f"mission budget exhausted: {exhausted}",
                rule="budget",
            )

        # --- permission ---------------------------------------------------
        verdict = self._permissions.check(capability, clean_params, context)
        await self._bus.publish(
            Event(
                type=ev.PERMISSION_CHECKED,
                source="permission-engine",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={
                    "capability": capability.name,
                    "mission_id": context.mission_id,
                    **verdict.to_dict(),
                },
            )
        )

        if verdict.decision is Decision.DENY:
            return await self._on_denied(capability, clean_params, context, verdict)

        if verdict.decision is Decision.REQUIRE_CONFIRMATION:
            return await self._on_confirmation_required(capability, clean_params, context, verdict)

        return await self._run(capability, clean_params, context, verdict, started)

    # -- outcome paths ------------------------------------------------------

    async def _reject(
        self,
        capability_name: str,
        params: dict[str, Any],
        context: ExecutionContext,
        detail: str,
    ) -> ExecutionResult:
        await self._bus.publish(
            Event(
                type=ev.COMMAND_REJECTED,
                source="execution-gateway",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={"capability": capability_name, "detail": detail},
            )
        )
        await self._audit.log(
            action="tool.reject",
            actor=context.actor,
            subject=capability_name,
            decision="invalid",
            correlation_id=context.correlation_id,
            mission_id=context.mission_id,
            detail=detail,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.INVALID, capability=capability_name, detail=detail
        )

    async def _deny_result(
        self,
        capability: Capability,
        params: dict[str, Any],
        context: ExecutionContext,
        *,
        detail: str,
        rule: str,
    ) -> ExecutionResult:
        await self._audit.log(
            action="tool.denied",
            actor=context.actor,
            subject=capability.name,
            decision="deny",
            correlation_id=context.correlation_id,
            mission_id=context.mission_id,
            level=capability.level.code,
            rule=rule,
            detail=detail,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.DENIED, capability=capability.name, detail=detail
        )

    async def _on_denied(
        self,
        capability: Capability,
        params: dict[str, Any],
        context: ExecutionContext,
        verdict: Verdict,
    ) -> ExecutionResult:
        await self._bus.publish(
            Event(
                type=ev.PERMISSION_DENIED,
                source="permission-engine",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                priority=Priority.URGENT,
                payload={
                    "capability": capability.name,
                    "mission_id": context.mission_id,
                    **verdict.to_dict(),
                },
            )
        )
        # Denials are always audited, at any level: a blocked action is the
        # interesting one.
        await self._audit.log(
            action="tool.denied",
            actor=context.actor,
            subject=capability.name,
            decision="deny",
            correlation_id=context.correlation_id,
            mission_id=context.mission_id,
            level=capability.level.code,
            rule=verdict.rule,
            detail=verdict.reason,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.DENIED,
            capability=capability.name,
            detail=verdict.reason,
            verdict=verdict,
        )

    async def _on_confirmation_required(
        self,
        capability: Capability,
        params: dict[str, Any],
        context: ExecutionContext,
        verdict: Verdict,
    ) -> ExecutionResult:
        record = self._permissions.approvals.request(
            fp=verdict.fingerprint,
            capability=capability.name,
            params=params,
            confirmation=verdict.confirmation,
            mission_id=context.mission_id,
        )
        await self._bus.publish(
            Event(
                type=ev.PERMISSION_CONFIRMATION_REQUIRED,
                source="permission-engine",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                priority=Priority.URGENT,
                payload={
                    "capability": capability.name,
                    "mission_id": context.mission_id,
                    "request": record,
                    **verdict.to_dict(),
                },
            )
        )
        await self._audit.log(
            action="tool.confirmation_required",
            actor=context.actor,
            subject=capability.name,
            decision="require_confirmation",
            correlation_id=context.correlation_id,
            mission_id=context.mission_id,
            level=capability.level.code,
            fingerprint=verdict.fingerprint,
            confirmation=str(verdict.confirmation),
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.AWAITING_CONFIRMATION,
            capability=capability.name,
            detail=verdict.reason,
            verdict=verdict,
        )

    async def _run(
        self,
        capability: Capability,
        params: dict[str, Any],
        context: ExecutionContext,
        verdict: Verdict,
        started: Any,
    ) -> ExecutionResult:
        await self._bus.publish(
            Event(
                type=ev.TOOL_INVOKED,
                source="execution-gateway",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={
                    "capability": capability.name,
                    "mission_id": context.mission_id,
                    "params": params,
                    "level": capability.level.code,
                },
            )
        )
        self._budgets.charge_tool_call(context.mission_id)

        # An approval is spent by the call it authorised.
        if verdict.rule == "approval_granted":
            self._permissions.approvals.consume(verdict.fingerprint)

        try:
            result = await capability.handler(params, context)
        except Exception as exc:
            log.exception("capability %s raised", capability.name)
            detail = f"{type(exc).__name__}: {exc}"
            await self._bus.publish(
                Event(
                    type=ev.TOOL_FAILED,
                    source="execution-gateway",
                    correlation_id=context.correlation_id,
                    device_id=context.device_id,
                    priority=Priority.URGENT,
                    payload={"capability": capability.name, "error": detail},
                )
            )
            await self._audit.log(
                action="tool.failed",
                actor=context.actor,
                subject=capability.name,
                decision="error",
                correlation_id=context.correlation_id,
                mission_id=context.mission_id,
                level=capability.level.code,
                detail=detail,
            )
            return ExecutionResult(
                outcome=ExecutionOutcome.FAILED,
                capability=capability.name,
                detail=detail,
                verdict=verdict,
                duration_ms=self._elapsed_ms(started),
            )

        # --- independent verification ------------------------------------
        await self._bus.publish(
            Event(
                type=ev.VERIFICATION_STARTED,
                source="verifier",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={"capability": capability.name, "mission_id": context.mission_id},
            )
        )
        report = await self._verifier.verify(capability, params, result, context)
        await self._bus.publish(
            Event(
                type=(ev.VERIFICATION_PASSED if report.goal_reached else ev.VERIFICATION_FAILED),
                source="verifier",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                priority=(Priority.NORMAL if report.goal_reached else Priority.URGENT),
                payload={"mission_id": context.mission_id, **report.to_dict()},
            )
        )
        await self._bus.publish(
            Event(
                type=ev.TOOL_SUCCEEDED,
                source="execution-gateway",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={
                    "capability": capability.name,
                    "mission_id": context.mission_id,
                    "verification": str(report.status),
                    # Who caused this action. The Trigger Watcher uses it to
                    # refuse to let one routine's action set off another,
                    # which is what keeps event-driven routines from forming
                    # a cycle (Blueprint 7.3's "Agent-Endlosschleife").
                    "actor": context.actor,
                    # Memory is a named Event Bus consumer (Blueprint 5.1) and
                    # needs the arguments to tell one habit from another:
                    # "Licht im Office an" is not the same routine as
                    # "Licht im Schlafzimmer an".
                    "params": params,
                },
            )
        )

        if capability.level >= AUDIT_THRESHOLD:
            await self._audit.log(
                action="tool.executed",
                actor=context.actor,
                subject=capability.name,
                decision="allow",
                correlation_id=context.correlation_id,
                prev_state=result.get("prev_state"),
                rollback_point=result.get("rollback_point"),
                mission_id=context.mission_id,
                level=capability.level.code,
                rule=verdict.rule,
                verification=str(report.status),
                reversible=capability.reversible,
            )

        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED,
            capability=capability.name,
            detail=report.detail,
            verdict=verdict,
            result=result,
            verification=report,
            duration_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _elapsed_ms(started: Any) -> float:
        return (utc_now() - started).total_seconds() * 1000.0
