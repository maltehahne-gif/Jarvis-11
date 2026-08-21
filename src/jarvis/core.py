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
from datetime import UTC, datetime, timedelta
from typing import Any

from jarvis.agents.coordinator import AgentCoordinator, AgentRun
from jarvis.agents.factory import build_provider
from jarvis.agents.provider import IntelligenceProvider
from jarvis.audit.logger import AuditLogger
from jarvis.capability.registry import CapabilityRegistry
from jarvis.config import CoreConfig
from jarvis.context.builder import ContextBuilder, Destination
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, new_id, utc_now
from jarvis.execution.budget import BudgetTracker
from jarvis.execution.gateway import ExecutionGateway, ExecutionOutcome, ExecutionResult
from jarvis.intent.router import ControlAction, Intent, IntentRouter, Route
from jarvis.memory.control import MemoryControl
from jarvis.memory.learning import RoutineProposal, TriggerKind
from jarvis.memory.service import MemoryService
from jarvis.mission.engine import MissionEngine
from jarvis.mission.model import Mission, MissionState, TaskState
from jarvis.mission.runner import MissionRunner, RunOutcome, StopReason
from jarvis.permission.engine import PermissionEngine
from jarvis.permission.policy import Confirmation, Policy
from jarvis.persistence.ports import Store
from jarvis.persistence.sqlite_store import SqliteStore
from jarvis.planner.plan import PlanError
from jarvis.planner.planner import PlannedMission, Planner
from jarvis.routing.model_router import ModelRouter, TaskClass
from jarvis.scheduler.jobs import (
    JobKind,
    JobOutcome,
    JobResult,
    ScheduledJob,
    parse_daily_at,
)
from jarvis.scheduler.scheduler import Scheduler
from jarvis.scheduler.triggers import TriggerWatcher
from jarvis.scheduler.watchdog import Watchdog
from jarvis.state.manager import StateManager
from jarvis.tools.mock import MockWorld, register_mock_tools
from jarvis.verify.verifier import Verifier
from jarvis.voice.mock import (
    RecordingAudioSink,
    ScriptedSpeechToText,
    ScriptedTextToSpeech,
    ScriptedWakeWord,
)
from jarvis.voice.pipeline import VoicePipeline
from jarvis.voice.ports import AudioSink, SpeechToText, TextToSpeech, WakeWordDetector
from jarvis.voice.presence import PresenceService

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
        wake: WakeWordDetector | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        sink: AudioSink | None = None,
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

        # Memory subscribes to the Bus rather than being called from the
        # command path - Blueprint 5.1 lists Memory among the Bus's consumers.
        self.memory = MemoryService(
            store=self.store,
            state_store=self.store,
            audit=self.audit,
            activator=self.activate_routine,
        )
        self.memory.attach_to_bus(self.bus)
        self.memory_control = MemoryControl(self.memory, audit=self.audit, bus=self.bus)
        self.context = ContextBuilder(self.memory)

        self.coordinator = AgentCoordinator(
            provider=provider or build_provider(self.config.provider),
            gateway=self.gateway,
            registry=self.registry,
            permissions=self.permissions,
            bus=self.bus,
            router=self.model_router,
            max_turns=self.config.max_agent_turns,
        )

        self.planner = Planner(self.registry, memory=self.memory, default_budget=self.config.budget)
        self.runner = MissionRunner(
            missions=self.missions,
            gateway=self.gateway,
            permissions=self.permissions,
            bus=self.bus,
            coordinator=self.coordinator,
        )
        self.scheduler = Scheduler(
            executor=self.run_scheduled_job,
            bus=self.bus,
            permissions=self.permissions,
            registry=self.registry,
            state_store=self.store,
            audit=self.audit,
        )
        self.watchdog = Watchdog(
            missions=self.missions,
            bus=self.bus,
            max_duration=self.config.budget.max_duration,
            audit=self.audit,
        )
        # The clock's counterpart: routines whose trigger is another action
        # completing rather than a time arriving (Blueprint 8.3).
        self.triggers = TriggerWatcher(
            scheduler=self.scheduler,
            bus=self.bus,
            audit=self.audit,
        )

        # Voice is an input surface, not an authority. A spoken command goes
        # through `handle_command` like any other, and Blueprint 7.2 keeps
        # voice identity a comfort signal rather than an authenticator.
        self.presence = PresenceService(self.state)
        self.voice = VoicePipeline(
            core=self,
            wake=wake or ScriptedWakeWord(),
            stt=stt or ScriptedSpeechToText(),
            tts=tts or ScriptedTextToSpeech(),
            sink=sink or RecordingAudioSink(),
            presence=self.presence,
            bus=self.bus,
        )
        self._started = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> list[Mission]:
        """Open storage and bring interrupted missions back honestly."""
        if self._started:
            return []
        await self.store.open()
        await self.state.load()
        # Privacy settings must survive a restart: a system that forgets the
        # owner switched learning off would start learning again on its own.
        await self.memory.load()
        await self.memory.purge_expired()
        await self.scheduler.load()
        resumed = await self.missions.resume_open_missions()
        if self.config.scheduler_enabled:
            await self.scheduler.start()
            await self.triggers.start()
        self._started = True
        log.info("core started; %d mission(s) resumed", len(resumed))
        return resumed

    async def stop(self) -> None:
        if not self._started:
            return
        await self.voice.stop()
        await self.triggers.stop()
        await self.scheduler.stop()
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

        # Refused at the door, not deep in the pipeline. Blueprint 7.2's kill
        # switch is meant to stop work, so the honest response to a new command
        # is to decline it before a mission exists - a mission created only to
        # be halted would show up in the HUD as work that never happened. The
        # Permission Engine denies these calls too (`test_permission.py`); this
        # is the earlier of two independent refusals, not a replacement.
        if self.permissions.kill_switch_engaged:
            await self.audit.log(
                action="command.refused",
                actor="core",
                subject=intent.capability or "command",
                decision="deny",
                correlation_id=correlation_id,
                rule="kill_switch",
                text=text,
            )
            await self.bus.publish(
                Event(
                    type=ev.COMMAND_REJECTED,
                    source="core",
                    correlation_id=correlation_id,
                    device_id=device_id,
                    priority=Priority.URGENT,
                    payload={"reason": "kill_switch", "text": text},
                )
            )
            return CommandResult(
                message="Kill Switch ist aktiv. Sag „weitermachen“, wenn ich wieder darf.",
                intent=intent,
                extra={"refused": "kill_switch"},
            )

        return await self._handle_planned(intent, correlation_id, device_id, grants)

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

    async def _handle_planned(
        self,
        intent: Intent,
        correlation_id: str,
        device_id: str | None,
        grants: frozenset[str],
    ) -> CommandResult:
        """Plan the goal, then run the plan - Blueprint figure 2's Plan/Route.

        Both a one-capability command and an open-ended goal come through here.
        They differ only in what the Planner produces: a single direct step for
        the first, a delegation step for the second, or a multi-step procedure
        when memory knows one. Everything after that - dependencies, permission
        checks, checkpoints, verification - is identical, which is the point of
        having a plan at all.
        """
        mission = await self._open_mission(intent, correlation_id, device_id)

        # Context is assembled before planning, so a remembered procedure and
        # the agent both see the same relevant world (Blueprint 5.1).
        routing = self.model_router.route(TaskClass.FEATURE, offline=self.config.offline)
        built = await self.context.build(
            intent.text,
            destination=Destination.for_provider(routing.provider),
            state=self.state.snapshot(),
        )

        try:
            planned = await self.planner.plan(
                intent.text,
                capability=intent.capability,
                params=intent.params,
            )
        except PlanError as exc:
            await self.missions.transition(mission, MissionState.FAILED, str(exc))
            await self._release(mission)
            return CommandResult(
                message=f"Dafür habe ich keinen ausführbaren Plan: {exc}",
                intent=intent,
                mission_id=mission.mission_id,
                mission_state=str(mission.state),
            )

        if not planned.executable:
            # Refusing up front beats starting something that provably cannot
            # finish and discovering it half-done (Blueprint 5.1, "Budget ...
            # berücksichtigen").
            reason = "; ".join(planned.budget_problems)
            await self.missions.transition(mission, MissionState.BLOCKED, reason)
            await self._release(mission)
            return CommandResult(
                message=f"Passt nicht ins Budget: {reason}",
                intent=intent,
                mission_id=mission.mission_id,
                mission_state=str(mission.state),
                extra={"plan": planned.to_dict()},
            )

        mission.context["plan"] = planned.to_dict()
        for task in planned.plan.to_tasks():
            mission.add_task(task)
        await self.missions.save(mission)

        await self.bus.publish(
            Event(
                type=ev.PLAN_CREATED,
                source="planner",
                correlation_id=correlation_id,
                device_id=device_id,
                payload={"mission_id": mission.mission_id, **planned.to_dict()},
            )
        )

        # Rights are scoped to exactly what the plan will call, and they expire
        # (Blueprint 7.2). A plan that delegates to an agent cannot name its
        # calls in advance, so it gets the wildcard - named scopes still gate
        # the sensitive capabilities behind it.
        named = frozenset(s.capability for s in planned.plan.steps if s.capability)
        needs_wildcard = any(s.capability is None for s in planned.plan.steps)
        self.permissions.issue_grant(
            mission.mission_id,
            capabilities=frozenset({"*"}) if needs_wildcard else named,
            grants=grants,
            ttl=self.config.grant_ttl,
            device_id=device_id,
        )
        self.gateway.budgets.start(mission.mission_id, planned.budget)

        await self.missions.transition(mission, MissionState.RUNNING, planned.plan.rationale)
        await self.state.mark_active(mission.mission_id)

        outcome = await self.runner.run(
            mission,
            grants=grants,
            device_id=device_id,
            actor="core",
            agent_context=built.to_dict(),
            offline=self.config.offline,
        )
        return await self._settle_run(
            mission, outcome, intent=intent, plan=planned, context_used=built
        )

    async def resume_mission(
        self, mission: Mission, *, grants: frozenset[str] = frozenset()
    ) -> CommandResult:
        """Continue a stopped mission from its last checkpoint.

        This is what checkpoints are for. A mission paused by the kill switch,
        killed by the watchdog or interrupted by a restart keeps its completed
        tasks; resuming re-runs only what is left. Every remaining step still
        goes through the Permission Engine - time has passed, grants have
        expired, and the world may have changed since the plan was made.
        """
        if mission.is_terminal and mission.state is not MissionState.FAILED:
            return CommandResult(
                message=f"Mission ist bereits {mission.state}.",
                mission_id=mission.mission_id,
                mission_state=str(mission.state),
            )

        if mission.state is MissionState.FAILED:
            # FAILED is terminal in the state machine, and rightly so - a
            # resumed run is a new attempt, not a continuation of the old one.
            return CommandResult(
                message=(
                    "Diese Mission ist fehlgeschlagen. Schick den Auftrag neu, "
                    "dann läuft nur der Rest des Plans."
                ),
                mission_id=mission.mission_id,
                mission_state=str(mission.state),
            )

        self.permissions.issue_grant(
            mission.mission_id,
            capabilities=frozenset(t.capability for t in mission.tasks if t.capability)
            or frozenset({"*"}),
            grants=grants,
            ttl=self.config.grant_ttl,
            device_id=mission.device_id,
        )
        self.gateway.budgets.start(mission.mission_id, self.config.budget)
        await self.state.mark_active(mission.mission_id)

        outcome = await self.runner.resume(mission, grants=grants, device_id=mission.device_id)
        return await self._settle_run(mission, outcome)

    async def activate_routine(self, proposal: RoutineProposal) -> str | None:
        """Turn an owner-approved routine into a standing job (Blueprint 8.3).

        This is where "erst nach Freigabe werden ... Automationen permanent"
        actually becomes permanent. The job inherits no rights beyond the
        default scope, and if its capability sits above the Scheduler's
        unattended ceiling it will park for confirmation on every firing
        rather than run - approving a *pattern* is not approving unattended
        execution of a sensitive action.

        Two kinds of trigger can be watched for, and they differ only in what
        does the watching: `DAILY` waits on the Scheduler's clock, `AFTER` on
        the Trigger Watcher's event stream. Both fire through the same
        `Scheduler`, so both are held to the same unattended limits.
        """
        if not proposal.schedulable:
            return None

        if proposal.trigger_kind is TriggerKind.AFTER:
            return await self._activate_after_routine(proposal)

        at = parse_daily_at(proposal.trigger_detail or "09h")
        first_run = datetime.combine(utc_now().date(), at, tzinfo=UTC)
        if first_run <= utc_now():
            first_run += timedelta(days=1)

        job = await self.scheduler.register(
            ScheduledJob(
                name=f"routine: {proposal.capability}",
                goal=f"{proposal.capability} ({proposal.trigger})",
                kind=JobKind.DAILY,
                capability=proposal.capability,
                params=dict(proposal.params),
                daily_at=at,
                next_run_at=first_run,
                origin=f"routine:{proposal.proposal_id}",
            )
        )
        return job.job_id

    async def _activate_after_routine(self, proposal: RoutineProposal) -> str | None:
        """Arm a routine on the completion of another capability.

        Refuses if the preceding capability is not registered. An `AFTER` job
        naming a capability that does not exist could never fire, and a job
        that silently never fires is the broken promise this whole path exists
        to avoid.
        """
        preceding = proposal.trigger_detail
        if not preceding or not self.registry.has(preceding):
            log.warning(
                "cannot arm routine %s: unknown preceding capability %r",
                proposal.proposal_id,
                preceding,
            )
            return None

        job = await self.scheduler.register(
            ScheduledJob(
                name=f"routine: {proposal.capability}",
                goal=f"{proposal.capability} ({proposal.trigger})",
                kind=JobKind.AFTER,
                capability=proposal.capability,
                params=dict(proposal.params),
                after_capability=preceding,
                origin=f"routine:{proposal.proposal_id}",
            )
        )
        return job.job_id

    async def run_scheduled_job(self, job: ScheduledJob) -> JobResult:
        """Execute one scheduled job as a background mission.

        The Scheduler has already refused anything above the unattended risk
        ceiling, but nothing here relies on that: every step still goes through
        the Permission Engine, and a call that turns out to need confirmation
        parks the mission rather than proceeding. Defence in depth, because the
        ceiling is a policy and the gate is a mechanism.
        """
        try:
            planned = await self.planner.plan(
                job.goal, capability=job.capability, params=job.params
            )
        except PlanError as exc:
            return JobResult(outcome=JobOutcome.FAILED, detail=str(exc))

        if not planned.executable:
            return JobResult(outcome=JobOutcome.FAILED, detail="; ".join(planned.budget_problems))

        mission = await self.missions.create(
            job.goal,
            device_id=job.device_id,
            context={"scheduled_job": job.job_id, "origin": job.origin, "unattended": True},
        )
        await self.missions.transition(mission, MissionState.PLANNING, f"job {job.name}")

        mission.context["plan"] = planned.to_dict()
        for task in planned.plan.to_tasks():
            mission.add_task(task)
        await self.missions.save(mission)

        # Exactly the grants the job was registered with - never more.
        named = frozenset(s.capability for s in planned.plan.steps if s.capability)
        needs_wildcard = any(s.capability is None for s in planned.plan.steps)
        self.permissions.issue_grant(
            mission.mission_id,
            capabilities=frozenset({"*"}) if needs_wildcard else named,
            grants=job.grants,
            ttl=self.config.grant_ttl,
            device_id=job.device_id,
        )
        self.gateway.budgets.start(mission.mission_id, planned.budget)

        await self.missions.transition(mission, MissionState.RUNNING, "scheduled run")
        await self.state.mark_active(mission.mission_id)

        outcome = await self.runner.run(
            mission, grants=job.grants, device_id=job.device_id, actor="scheduler"
        )
        settled = await self._settle_run(mission, outcome, plan=planned)

        if outcome.pending_approval is not None:
            return JobResult(
                outcome=JobOutcome.PARKED,
                mission_id=mission.mission_id,
                detail=outcome.pending_approval.get("reason", "needs confirmation"),
            )
        if outcome.succeeded:
            return JobResult(
                outcome=JobOutcome.SUCCEEDED,
                mission_id=mission.mission_id,
                detail=settled.message,
            )
        return JobResult(
            outcome=JobOutcome.FAILED,
            mission_id=mission.mission_id,
            detail=settled.message,
        )

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

        # Resume the plan rather than calling the gateway once directly. A
        # plan can have steps after the one that needed confirmation, and this
        # is what carries on to them - the same reason `resume_mission` uses
        # the runner instead of a single gateway call. It also means the
        # approved task's own state actually reaches DONE/FAILED, which a
        # bare `gateway.execute()` here never touched, leaving the task stuck
        # on PENDING under a COMPLETED mission.
        #
        # No fresh grant is issued: the mission's grant from when the plan
        # started is still active (`_settle_run` only releases it once the
        # mission leaves WAITING_FOR_APPROVAL), and the Permission Engine
        # unions it into every check regardless of what `grants` a call
        # passes. Approval only had to clear the confirmation gate, which
        # `approvals.grant` above already did.
        await self.missions.transition(mission, MissionState.RUNNING, "owner approved")
        await self.state.mark_active(mission.mission_id)
        outcome = await self.runner.run(
            mission, grants=frozenset(), device_id=device_id, actor="owner"
        )
        return await self._settle_run(mission, outcome)

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

    async def _settle_run(
        self,
        mission: Mission,
        outcome: RunOutcome,
        *,
        intent: Intent | None = None,
        plan: PlannedMission | None = None,
        context_used: Any = None,
    ) -> CommandResult:
        """Bring a finished plan run to rest and describe what happened.

        The stop reason decides the resting state, and each one lands somewhere
        the owner can act on: an unfinished plan is never quietly reported as
        done, and a mission stopped by the kill switch is PAUSED rather than
        FAILED, because it can be resumed from its checkpoint.
        """
        last = outcome.executions[-1] if outcome.executions else None
        agent_run = outcome.agent_runs[-1] if outcome.agent_runs else None
        pending_approval: dict[str, Any] | None = None
        message: str

        if outcome.pending_approval is not None:
            pending_approval = outcome.pending_approval
            await self.missions.transition(
                mission, MissionState.WAITING_FOR_APPROVAL, pending_approval["reason"]
            )
            message = f"Bestätigung nötig: {pending_approval['reason']}"

        elif outcome.stopped_reason == StopReason.KILL_SWITCH:
            await self.missions.transition(mission, MissionState.PAUSED, "kill switch engaged")
            message = "Gestoppt."

        elif outcome.stopped_reason.startswith("budget:"):
            limit = outcome.stopped_reason.removeprefix("budget:")
            await self.missions.transition(
                mission, MissionState.FAILED, f"budget exhausted: {limit}"
            )
            message = f"Budget erschöpft ({limit})."

        elif outcome.stopped_reason == StopReason.BLOCKED:
            await self.missions.transition(
                mission, MissionState.BLOCKED, "a step it depended on did not complete"
            )
            message = "Blockiert: ein vorheriger Schritt ist nicht durchgelaufen."

        elif last is not None and last.outcome is ExecutionOutcome.DENIED:
            await self.missions.transition(mission, MissionState.BLOCKED, last.detail)
            message = f"Blockiert: {last.detail}"

        elif not outcome.executions and not outcome.completed:
            await self.missions.transition(
                mission, MissionState.FAILED, "no executable step was produced"
            )
            message = "Dafür habe ich keinen ausführbaren Schritt gefunden."

        else:
            # Verification, not the tool's own word, decides whether it is done.
            await self.missions.transition(mission, MissionState.VERIFYING, "checking outcome")
            if outcome.succeeded:
                await self.missions.transition(mission, MissionState.COMPLETED, "goal verified")
                message = "Erledigt."
            else:
                detail = self._failure_detail(outcome)
                await self.missions.transition(mission, MissionState.FAILED, detail)
                message = f"Nicht erreicht: {detail}"

        if mission.state is not MissionState.WAITING_FOR_APPROVAL:
            await self._release(mission)

        extra: dict[str, Any] = {"run": outcome.to_dict()}
        if plan is not None:
            extra["plan"] = plan.to_dict()
        if context_used is not None:
            extra["context"] = context_used.to_dict()

        return CommandResult(
            message=message,
            intent=intent,
            mission_id=mission.mission_id,
            mission_state=str(mission.state),
            execution=last,
            agent_run=agent_run,
            pending_approval=pending_approval,
            extra=extra,
        )

    @staticmethod
    def _failure_detail(outcome: RunOutcome) -> str:
        for execution in reversed(outcome.executions):
            if execution.verification is not None and not execution.verification.goal_reached:
                return (
                    f"tool reported success but verification failed: "
                    f"{execution.verification.detail}"
                )
            if execution.outcome is ExecutionOutcome.FAILED:
                return execution.detail
            if execution.outcome is ExecutionOutcome.INVALID:
                return f"ungültiger Aufruf: {execution.detail}"
        return outcome.stopped_reason

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
