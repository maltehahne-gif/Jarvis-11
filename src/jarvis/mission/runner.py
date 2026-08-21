"""Mission Runner - Blueprint 5.1, 5.3, 7.3 and 9.2.

Walks a mission's task graph, respecting dependencies, and turns it into
executed work. Everything it does that is not "call the next step" comes from a
specific blueprint requirement:

* **Dependencies** (5.1). A step runs only when everything it depends on is
  DONE. A failed predecessor does not become "done enough" - its dependents
  are unreachable and the mission is honest about that rather than carrying on
  as though the missing step had happened.
* **Checkpoints** (5.1, 7.3). After every step, progress is written before
  anything else happens. A run that stops - crash, kill switch, exhausted
  budget - resumes from the checkpoint instead of redoing completed work. This
  is also the retry mechanism: re-running a mission re-runs only what is left.
* **Kill switch and budgets** (7.2, 7.3). Both are checked before *every*
  step, not once per run. "Jarvis, stop everything" has to stop a mission
  mid-plan, and a plan that loops must run out of budget rather than run
  forever.
* **Heartbeat** (9.2). "Long mission status: sichtbares Event spätestens alle
  1-3 s, solange Aktivität besteht." A slow agent step would otherwise leave
  the HUD silent, which reads as a hang. The heartbeat runs only while a run is
  active, so an idle system stays quiet.

The runner never decides what is allowed. Every step goes through the
Execution Gateway, which re-checks permission and re-validates parameters -
including for steps that came from a plan the owner already approved, because
approval of a plan is not approval of each action within it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis.agents.coordinator import AgentCoordinator, AgentRun
from jarvis.capability.models import ExecutionContext
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority
from jarvis.execution.gateway import ExecutionGateway, ExecutionOutcome, ExecutionResult
from jarvis.mission.engine import MissionEngine
from jarvis.mission.model import Mission, MissionState, Task, TaskState
from jarvis.permission.engine import PermissionEngine

log = logging.getLogger(__name__)

#: Blueprint 9.2 asks for a visible event every 1-3 seconds while a mission is
#: active. Two sits in the middle of that window.
HEARTBEAT_SECONDS = 2.0


class StopReason:
    PLAN_COMPLETE = "plan_complete"
    AWAITING_APPROVAL = "awaiting_approval"
    KILL_SWITCH = "kill_switch"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(slots=True)
class RunOutcome:
    """What one pass over a mission's plan achieved."""

    mission_id: str
    stopped_reason: str
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    executions: list[ExecutionResult] = field(default_factory=list)
    agent_runs: list[AgentRun] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.stopped_reason == StopReason.PLAN_COMPLETE and not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "stopped_reason": self.stopped_reason,
            "completed": list(self.completed),
            "failed": list(self.failed),
            "executions": [e.to_dict() for e in self.executions],
            "pending_approval": self.pending_approval,
            "succeeded": self.succeeded,
        }


class MissionRunner:
    def __init__(
        self,
        *,
        missions: MissionEngine,
        gateway: ExecutionGateway,
        permissions: PermissionEngine,
        bus: EventBus,
        coordinator: AgentCoordinator | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._missions = missions
        self._gateway = gateway
        self._permissions = permissions
        self._bus = bus
        self._coordinator = coordinator
        self._heartbeat_seconds = heartbeat_seconds

    async def run(
        self,
        mission: Mission,
        *,
        grants: frozenset[str] = frozenset(),
        device_id: str | None = None,
        actor: str = "mission-runner",
        agent_context: dict[str, Any] | None = None,
        offline: bool = True,
    ) -> RunOutcome:
        """Execute every ready step until the plan finishes or stops.

        `agent_context` is what the Context Builder assembled for this goal.
        The runner passes it through untouched - it does not decide what a
        reasoner may see, because that decision depends on where the request
        is going and belongs upstream (Blueprint 5.1, Principle 3).
        """
        outcome = RunOutcome(mission_id=mission.mission_id, stopped_reason=StopReason.PLAN_COMPLETE)
        heartbeat = asyncio.create_task(self._heartbeat(mission))

        try:
            while True:
                halt = self._halt_reason(mission)
                if halt is not None:
                    outcome.stopped_reason = halt
                    break

                ready = mission.ready_tasks()
                if not ready:
                    outcome.stopped_reason = (
                        StopReason.PLAN_COMPLETE if mission.is_plan_finished else StopReason.BLOCKED
                    )
                    break

                # One at a time. Blueprint 6.3: parallel agents are only worth
                # it when sub-problems are genuinely independent, and even then
                # sequential execution is the safe default until there is a
                # measured reason to change it.
                task = ready[0]
                await self._execute(
                    mission,
                    task,
                    outcome,
                    grants,
                    device_id,
                    actor,
                    agent_context=agent_context,
                    offline=offline,
                )

                # Persist progress before anything else can go wrong.
                mission.checkpoint(f"after {task.description}")
                await self._missions.save(mission)

                if outcome.pending_approval is not None:
                    outcome.stopped_reason = StopReason.AWAITING_APPROVAL
                    break
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        if outcome.failed and outcome.stopped_reason == StopReason.PLAN_COMPLETE:
            outcome.stopped_reason = StopReason.FAILED

        await self._announce_finished(mission, outcome)
        return outcome

    # -- one step -----------------------------------------------------------

    async def _execute(
        self,
        mission: Mission,
        task: Task,
        outcome: RunOutcome,
        grants: frozenset[str],
        device_id: str | None,
        actor: str,
        agent_context: dict[str, Any] | None = None,
        offline: bool = True,
    ) -> None:
        task.state = TaskState.RUNNING
        await self._bus.publish(
            Event(
                type=ev.MISSION_TASK_STARTED,
                source="mission-runner",
                correlation_id=mission.correlation_id,
                device_id=device_id,
                payload={
                    "mission_id": mission.mission_id,
                    "task_id": task.task_id,
                    "description": task.description,
                    "capability": task.capability,
                },
            )
        )

        context = ExecutionContext(
            correlation_id=mission.correlation_id,
            mission_id=mission.mission_id,
            device_id=device_id,
            actor=actor,
            grants=grants,
        )

        if task.capability is not None:
            result = await self._gateway.execute(task.capability, task.params, context)
            outcome.executions.append(result)
            task.result = result.to_dict()
            task.state = self._state_for(result)

            if result.outcome is ExecutionOutcome.AWAITING_CONFIRMATION:
                assert result.verdict is not None
                outcome.pending_approval = {
                    "fingerprint": result.verdict.fingerprint,
                    "capability": result.capability,
                    "confirmation": str(result.verdict.confirmation),
                    "reason": result.detail,
                    "task_id": task.task_id,
                }
        elif self._coordinator is not None:
            run = await self._coordinator.run(
                task.description,
                context,
                offline=offline,
                extra_context=agent_context,
            )
            outcome.agent_runs.append(run)
            outcome.executions.extend(run.executions)
            task.result = run.to_dict()
            task.state = TaskState.DONE if run.succeeded else TaskState.FAILED
            if run.awaiting_confirmation:
                task.state = TaskState.PENDING
                pending = next(
                    (
                        e
                        for e in run.executions
                        if e.outcome is ExecutionOutcome.AWAITING_CONFIRMATION
                    ),
                    None,
                )
                if pending is not None and pending.verdict is not None:
                    outcome.pending_approval = {
                        "fingerprint": pending.verdict.fingerprint,
                        "capability": pending.capability,
                        "confirmation": str(pending.verdict.confirmation),
                        "reason": pending.detail,
                        "task_id": task.task_id,
                    }
        else:
            # A plan asked for reasoning and no reasoner is wired in. Failing
            # is the honest outcome; pretending the step succeeded would be
            # exactly the "falsches 'fertig'" the threat model warns about.
            task.state = TaskState.FAILED
            task.result = {"error": "no agent coordinator available for this step"}

        if task.state is TaskState.DONE:
            outcome.completed.append(task.task_id)
        elif task.state is TaskState.FAILED:
            outcome.failed.append(task.task_id)
            await self._skip_unreachable(mission, device_id)

        await self._bus.publish(
            Event(
                type=ev.MISSION_TASK_FINISHED,
                source="mission-runner",
                correlation_id=mission.correlation_id,
                device_id=device_id,
                priority=(Priority.URGENT if task.state is TaskState.FAILED else Priority.NORMAL),
                payload={
                    "mission_id": mission.mission_id,
                    "task_id": task.task_id,
                    "state": str(task.state),
                    "capability": task.capability,
                },
            )
        )

    async def _skip_unreachable(self, mission: Mission, device_id: str | None) -> None:
        """Mark steps that can never run now that a predecessor has failed.

        Leaving them PENDING would make the mission look like it still had
        work to do. Marking them SKIPPED says what actually happened.
        """
        for task in mission.blocked_tasks:
            task.state = TaskState.SKIPPED
            task.result = {"skipped": "a step it depended on did not complete"}
            await self._bus.publish(
                Event(
                    type=ev.MISSION_TASK_FINISHED,
                    source="mission-runner",
                    correlation_id=mission.correlation_id,
                    device_id=device_id,
                    payload={
                        "mission_id": mission.mission_id,
                        "task_id": task.task_id,
                        "state": str(task.state),
                    },
                )
            )

    # -- guards -------------------------------------------------------------

    def _halt_reason(self, mission: Mission) -> str | None:
        """Checked before every step, never once per run."""
        if self._permissions.kill_switch_engaged:
            return StopReason.KILL_SWITCH
        exhausted = self._gateway.budgets.exceeded(mission.mission_id)
        if exhausted is not None:
            return f"budget:{exhausted}"
        return None

    @staticmethod
    def _state_for(result: ExecutionResult) -> TaskState:
        if result.outcome is ExecutionOutcome.EXECUTED:
            return TaskState.DONE if result.succeeded else TaskState.FAILED
        if result.outcome is ExecutionOutcome.AWAITING_CONFIRMATION:
            return TaskState.PENDING
        return TaskState.FAILED

    # -- progress reporting -------------------------------------------------

    async def _heartbeat(self, mission: Mission) -> None:
        """Emit progress while a run is active (Blueprint 9.2)."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                await self._bus.publish(
                    Event(
                        type=ev.MISSION_PROGRESS,
                        source="mission-runner",
                        correlation_id=mission.correlation_id,
                        device_id=mission.device_id,
                        priority=Priority.BACKGROUND,
                        payload=self.progress(mission),
                    )
                )
        except asyncio.CancelledError:
            raise

    @staticmethod
    def progress(mission: Mission) -> dict[str, Any]:
        """The shape the HUD's mission view needs (Blueprint 3.2)."""
        total = len(mission.tasks)
        done = len(mission.completed_task_ids)
        return {
            "mission_id": mission.mission_id,
            "goal": mission.goal,
            "state": str(mission.state),
            "tasks_total": total,
            "tasks_done": done,
            "tasks_failed": sum(1 for t in mission.tasks if t.state is TaskState.FAILED),
            "tasks_skipped": sum(1 for t in mission.tasks if t.state is TaskState.SKIPPED),
            "fraction_done": (done / total) if total else 0.0,
            "checkpoints": len(mission.checkpoints),
        }

    async def _announce_finished(self, mission: Mission, outcome: RunOutcome) -> None:
        await self._bus.publish(
            Event(
                type=ev.MISSION_PROGRESS,
                source="mission-runner",
                correlation_id=mission.correlation_id,
                device_id=mission.device_id,
                payload={**self.progress(mission), "stopped_reason": outcome.stopped_reason},
            )
        )

    # -- resumption ---------------------------------------------------------

    async def resume(
        self,
        mission: Mission,
        *,
        grants: frozenset[str] = frozenset(),
        device_id: str | None = None,
    ) -> RunOutcome:
        """Continue a mission from where its checkpoint says it stopped.

        Nothing special is needed to skip completed work: DONE tasks are not
        ready tasks, so the runner simply never picks them up again. A task
        left RUNNING by a crash is put back to PENDING - it did not finish, and
        the gateway will re-check permission before it runs again.
        """
        for task in mission.tasks:
            if task.state is TaskState.RUNNING:
                task.state = TaskState.PENDING

        if mission.state is not MissionState.RUNNING:
            await self._missions.transition(
                mission, MissionState.RUNNING, "resumed from checkpoint"
            )
        return await self.run(mission, grants=grants, device_id=device_id, actor="resume")
