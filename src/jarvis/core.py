"""JARVIS Core - the assembly point.

Blueprint 4.2 asks for a modular monolith: "Ein Python-Core-Prozess enthält klar
getrennte Module und stabile Interfaces." This module is that process. It owns
no domain logic of its own; it wires the modules together and runs the pipeline
from Blueprint figure 2:

    Intent/Event -> Build Context -> Plan/Route -> Permission Check
      -> Execute Tool/Agent -> Verify Outcome -> Update Memory+State
      -> Voice/HUD/Notification

Every command becomes a mission. Even "switch the light on" gets one, because a
mission is the unit that carries the correlation id, the expiring capability
grant and the audit trail - and Blueprint 7.2 wants those on every action, not
only on long-running ones. Cheap commands simply pass through the state machine
quickly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from jarvis.agents.coordinator import AgentCoordinator, AgentRun
from jarvis.agents.provider import IntelligenceProvider
from jarvis.agents.rule_provider import RuleBasedProvider
from jarvis.audit.logger import AuditLogger
from jarvis.capability.models import ExecutionContext
from jarvis.capability.registry import CapabilityRegistry
from jarvis.config import CoreConfig
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, new_id
from jarvis.execution.budget import BudgetTracker
from jarvis.execution.gateway import ExecutionGateway, ExecutionOutcome, ExecutionResult
from jarvis.intent.router import ControlAction, Intent, IntentRouter, Route
from jarvis.mission.engine import MissionEngine
from jarvis.mission.model import Mission, MissionState, Task, TaskState
from jarvis.permission.engine import PermissionEngine
from jarvis.permission.policy import Confirmation, Policy
from jarvis.persistence.ports import Store
from jarvis.persistence.sqlite_store import SqliteStore
from jarvis.routing.model_router import ModelRouter
from jarvis.state.manager import StateManager
from jarvis.tools.mock import MockWorld, register_mock_tools
from jarvis.verify.verifier import Verifier

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CommandResult:
    """What the Core reports back for one command."""

    message: str
    intent: Intent | None = None
    mission_id: str | None = None
    mission_state: str | None = None
    execution: ExecutionResult | None = None
    agent_run: AgentRun | None = None
    pending_approval: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "intent": self.intent.to_dict() if self.intent else None,
            "mission_id": self.mission_id,
            "mission_state": self.mission_state,
            "execution": self.execution.to_dict() if self.execution else None,
            "agent_run": self.agent_run.to_dict() if self.agent_run else None,
            "pending_approval": self.pending_approval,
            **self.extra,
        }


class JarvisCore:
    def __init__(
        self,
        config: CoreConfig | None = None,
        *,
        store: Store | None = None,
        provider: IntelligenceProvider | None = None,
    ) -> None:
        self.config = config or CoreConfig()

        self.store: Store = store or SqliteStore(self.config.db_path)
        self.bus = EventBus(sink=self.store.append_event)
        self.audit = AuditLogger(self.store)
        self.registry = CapabilityRegistry()
        self.world: MockWorld = register_mock_tools(self.registry)

        self.permissions = PermissionEngine(
            Policy.default(
                trusted_devices=self.config.trusted_devices,
                blocked_capabilities=self.config.blocked_capabilities,
            )
        )
        self.permissions.policy.default_grant_ttl = self.config.grant_ttl

        self.gateway = ExecutionGateway(
            registry=self.registry,
            permissions=self.permissions,
            bus=self.bus,
            audit=self.audit,
            verifier=Verifier(),
            budgets=BudgetTracker(self.config.budget),
        )
        self.missions = MissionEngine(store=self.store, bus=self.bus, audit=self.audit)
        self.state = StateManager(self.store)
        self.router = IntentRouter(self.registry)
        self.model_router = ModelRouter()
        self.coordinator = AgentCoordinator(
            provider=provider or RuleBasedProvider(),
            gateway=self.gateway,
            registry=self.registry,
            permissions=self.permissions,
            bus=self.bus,
            router=self.model_router,
            max_turns=self.config.max_agent_turns,
        )
        self._started = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> list[Mission]:
        """Open storage and bring interrupted missions back honestly."""
        if self._started:
            return []
        await self.store.open()
        await self.state.load()
        resumed = await self.missions.resume_open_missions()
        self._started = True
        log.info("core started; %d mission(s) resumed", len(resumed))
        return resumed

    async def stop(self) -> None:
        if not self._started:
            return
        await self.store.close()
        self._started = False

    # -- commands -----------------------------------------------------------

    async def handle_command(
        self,
        text: str,
        *,
        device_id: str | None = None,
        grants: frozenset[str] = frozenset(),
    ) -> CommandResult:
        """Run one text command through the full pipeline."""
        correlation_id = new_id()
        await self.bus.publish(
            Event(
                type=ev.COMMAND_RECEIVED,
                source="api",
                correlation_id=correlation_id,
                device_id=device_id,
                payload={"text": text},
            )
        )
        self.state.remember_turn("owner", text)

        intent = self.router.route(text)
        await self.bus.publish(
            Event(
                type=ev.COMMAND_ROUTED,
                source="intent-router",
                correlation_id=correlation_id,
                device_id=device_id,
                payload=intent.to_dict(),
            )
        )

        if intent.route is Route.CONTROL:
            return await self._handle_control(intent, correlation_id, device_id)
        if intent.route is Route.LOCAL_TOOL:
            return await self._handle_local_tool(intent, correlation_id, device_id, grants)
        return await self._handle_agent(intent, correlation_id, device_id, grants)

    async def _handle_control(
        self, intent: Intent, correlation_id: str, device_id: str | None
    ) -> CommandResult:
        """Control commands bypass planning entirely - Blueprint 7.2."""
        if intent.control is ControlAction.STOP_EVERYTHING:
            self.permissions.engage_kill_switch("owner said stop")
            await self.bus.publish(
                Event(
                    type=ev.KILL_SWITCH_ENGAGED,
                    source="core",
                    correlation_id=correlation_id,
                    device_id=device_id,
                    priority=Priority.CRITICAL,
                    payload={"reason": "owner said stop"},
                )
            )
            await self.audit.log(
                action="safety.kill_switch",
                actor="owner",
                subject="core",
                decision="engaged",
                correlation_id=correlation_id,
            )
            paused = await self._pause_active_missions("kill switch engaged")
            return CommandResult(
                message="Alles gestoppt.",
                intent=intent,
                extra={"kill_switch": True, "paused_missions": paused},
            )

        self.permissions.release_kill_switch()
        await self.bus.publish(
            Event(
                type=ev.KILL_SWITCH_RELEASED,
                source="core",
                correlation_id=correlation_id,
                device_id=device_id,
                priority=Priority.URGENT,
                payload={},
            )
        )
        await self.audit.log(
            action="safety.kill_switch",
            actor="owner",
            subject="core",
            decision="released",
            correlation_id=correlation_id,
        )
        return CommandResult(message="Weiter.", intent=intent, extra={"kill_switch": False})

    async def _handle_local_tool(
        self,
        intent: Intent,
        correlation_id: str,
        device_id: str | None,
        grants: frozenset[str],
    ) -> CommandResult:
        assert intent.capability is not None
        mission = await self._open_mission(intent, correlation_id, device_id)
        task = await self.missions.add_task(
            mission,
            Task(
                description=intent.text,
                capability=intent.capability,
                params=intent.params,
            ),
        )

        # Rights are scoped to this mission and this capability, and they
        # expire (Blueprint 7.2).
        self.permissions.issue_grant(
            mission.mission_id,
            capabilities=frozenset({intent.capability}),
            grants=grants,
            ttl=self.config.grant_ttl,
            device_id=device_id,
        )
        self.gateway.budgets.start(mission.mission_id, self.config.budget)

        await self.missions.transition(mission, MissionState.RUNNING, "local tool dispatch")
        await self.state.mark_active(mission.mission_id)

        context = ExecutionContext(
            correlation_id=correlation_id,
            mission_id=mission.mission_id,
            device_id=device_id,
            actor="intent-router",
            grants=grants,
        )
        result = await self.gateway.execute(intent.capability, intent.params, context)
        task.state = self._task_state_for(result)
        task.result = result.to_dict()

        return await self._settle(mission, result=result, task_note=intent.text)

    async def _handle_agent(
        self,
        intent: Intent,
        correlation_id: str,
        device_id: str | None,
        grants: frozenset[str],
    ) -> CommandResult:
        mission = await self._open_mission(intent, correlation_id, device_id)

        # The agent may reach for any registered capability, but named scopes
        # still gate the sensitive ones: without an explicit `comms` or
        # `secrets` grant, those calls are denied by the Permission Engine.
        self.permissions.issue_grant(
            mission.mission_id,
            capabilities=frozenset({"*"}),
            grants=grants,
            ttl=self.config.grant_ttl,
            device_id=device_id,
        )
        self.gateway.budgets.start(mission.mission_id, self.config.budget)

        await self.missions.transition(mission, MissionState.RUNNING, "agent planning")
        await self.state.mark_active(mission.mission_id)

        context = ExecutionContext(
            correlation_id=correlation_id,
            mission_id=mission.mission_id,
            device_id=device_id,
            actor="agent-coordinator",
            grants=grants,
        )
        run = await self.coordinator.run(intent.text, context)

        for execution in run.executions:
            mission.add_task(
                Task(
                    description=f"agent: {execution.capability}",
                    capability=execution.capability,
                    state=self._task_state_for(execution),
                    result=execution.to_dict(),
                )
            )
        await self.missions.save(mission)

        last = run.executions[-1] if run.executions else None
        return await self._settle(mission, result=last, agent_run=run, task_note=intent.text)

    # -- approvals ----------------------------------------------------------

    async def approve(
        self,
        fingerprint: str,
        *,
        confirmation: Confirmation = Confirmation.SIMPLE,
        device_id: str | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> CommandResult:
        """Record the owner's confirmation and re-run the pending action."""
        pending = next(
            (p for p in self.permissions.approvals.pending() if p["fingerprint"] == fingerprint),
            None,
        )
        if pending is None:
            return CommandResult(message="Keine offene Freigabe mit dieser Kennung.")

        self.permissions.approvals.grant(
            fingerprint, confirmation=confirmation, device_id=device_id, ttl=ttl
        )

        mission_id = pending.get("mission_id")
        mission = await self.missions.load(mission_id) if mission_id else None
        if mission is None:
            return CommandResult(message="Zugehörige Mission nicht gefunden.")

        await self.bus.publish(
            Event(
                type=ev.MISSION_APPROVAL_GRANTED,
                source="core",
                correlation_id=mission.correlation_id,
                device_id=device_id,
                payload={"mission_id": mission.mission_id, "fingerprint": fingerprint},
            )
        )
        await self.audit.log(
            action="approval.granted",
            actor="owner",
            subject=pending["capability"],
            decision="allow",
            correlation_id=mission.correlation_id,
            mission_id=mission.mission_id,
            fingerprint=fingerprint,
            confirmation=str(confirmation),
        )

        await self.missions.transition(mission, MissionState.RUNNING, "owner approved")
        context = ExecutionContext(
            correlation_id=mission.correlation_id,
            mission_id=mission.mission_id,
            device_id=device_id,
            actor="owner",
        )
        result = await self.gateway.execute(pending["capability"], pending["params"], context)
        return await self._settle(mission, result=result, task_note="approved")

    async def deny(self, fingerprint: str) -> CommandResult:
        pending = next(
            (p for p in self.permissions.approvals.pending() if p["fingerprint"] == fingerprint),
            None,
        )
        self.permissions.approvals.deny(fingerprint)
        if pending is None:
            return CommandResult(message="Keine offene Freigabe mit dieser Kennung.")

        mission_id = pending.get("mission_id")
        mission = await self.missions.load(mission_id) if mission_id else None
        if mission is not None:
            await self.bus.publish(
                Event(
                    type=ev.MISSION_APPROVAL_DENIED,
                    source="core",
                    correlation_id=mission.correlation_id,
                    payload={"mission_id": mission.mission_id, "fingerprint": fingerprint},
                )
            )
            await self.audit.log(
                action="approval.denied",
                actor="owner",
                subject=pending["capability"],
                decision="deny",
                correlation_id=mission.correlation_id,
                mission_id=mission.mission_id,
                fingerprint=fingerprint,
            )
            await self.missions.transition(mission, MissionState.CANCELED, "owner denied")
            await self._release(mission)
            return CommandResult(
                message="Abgelehnt.",
                mission_id=mission.mission_id,
                mission_state=str(mission.state),
            )
        return CommandResult(message="Abgelehnt.")

    # -- helpers ------------------------------------------------------------

    async def _open_mission(
        self, intent: Intent, correlation_id: str, device_id: str | None
    ) -> Mission:
        mission = await self.missions.create(
            intent.text,
            correlation_id=correlation_id,
            device_id=device_id,
            context={"route": str(intent.route), "confidence": intent.confidence},
        )
        await self.missions.transition(mission, MissionState.PLANNING, "intent routed")
        return mission

    @staticmethod
    def _task_state_for(result: ExecutionResult) -> TaskState:
        if result.outcome is ExecutionOutcome.EXECUTED:
            return TaskState.DONE if result.succeeded else TaskState.FAILED
        if result.outcome is ExecutionOutcome.AWAITING_CONFIRMATION:
            return TaskState.PENDING
        return TaskState.FAILED

    async def _settle(
        self,
        mission: Mission,
        *,
        result: ExecutionResult | None,
        agent_run: AgentRun | None = None,
        task_note: str = "",
    ) -> CommandResult:
        """Move the mission to its resting state and describe the outcome."""
        pending_approval: dict[str, Any] | None = None
        message: str

        if result is None:
            await self.missions.transition(
                mission, MissionState.FAILED, "no executable step was produced"
            )
            message = "Dafür habe ich keinen ausführbaren Schritt gefunden."

        elif result.outcome is ExecutionOutcome.AWAITING_CONFIRMATION:
            await self.missions.transition(
                mission, MissionState.WAITING_FOR_APPROVAL, result.detail
            )
            assert result.verdict is not None
            pending_approval = {
                "fingerprint": result.verdict.fingerprint,
                "capability": result.capability,
                "confirmation": str(result.verdict.confirmation),
                "reason": result.detail,
            }
            message = f"Bestätigung nötig: {result.detail}"

        elif result.outcome is ExecutionOutcome.DENIED:
            await self.missions.transition(mission, MissionState.BLOCKED, result.detail)
            message = f"Blockiert: {result.detail}"

        elif result.outcome is ExecutionOutcome.INVALID:
            await self.missions.transition(mission, MissionState.FAILED, result.detail)
            message = f"Ungültiger Aufruf: {result.detail}"

        elif result.outcome is ExecutionOutcome.FAILED:
            await self.missions.transition(mission, MissionState.FAILED, result.detail)
            message = f"Fehlgeschlagen: {result.detail}"

        else:
            # Executed. Verification decides whether it is actually done.
            await self.missions.transition(mission, MissionState.VERIFYING, "checking outcome")
            if result.succeeded:
                await self.missions.transition(mission, MissionState.COMPLETED, "goal verified")
                message = "Erledigt."
            else:
                detail = result.verification.detail if result.verification else ""
                await self.missions.transition(
                    mission,
                    MissionState.FAILED,
                    f"tool reported success but verification failed: {detail}",
                )
                message = f"Nicht erreicht: {detail}"

        if mission.state is not MissionState.WAITING_FOR_APPROVAL:
            await self._release(mission)

        return CommandResult(
            message=message,
            mission_id=mission.mission_id,
            mission_state=str(mission.state),
            execution=result,
            agent_run=agent_run,
            pending_approval=pending_approval,
        )

    async def _release(self, mission: Mission) -> None:
        """Hand back mission-scoped rights as soon as the mission rests."""
        self.permissions.revoke_grant(mission.mission_id)
        self.gateway.budgets.clear(mission.mission_id)
        await self.state.mark_inactive(mission.mission_id)

    async def _pause_active_missions(self, reason: str) -> list[str]:
        paused: list[str] = []
        for mission in await self.missions.list():
            if mission.state is MissionState.RUNNING:
                await self.missions.transition(mission, MissionState.PAUSED, reason)
                paused.append(mission.mission_id)
        return paused
