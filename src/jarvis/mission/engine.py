"""Mission Engine - Blueprint 5.1 and 5.3.

"Lang laufende Ziele in Tasks, Dependencies, Checkpoints und Status umwandeln."

The engine owns the invariant that makes missions trustworthy: **persist first,
then announce.** Every transition is written to the store before its event is
published, so a crash between the two can only lose the notification, never the
fact. Restart reads the stored state back and the mission continues from where
it actually was - which is what DoD 5.4's "Mission bleibt nach Prozessneustart
erhalten" is asking for.

Missions are cached in memory for the running process, but the store is the
source of truth; `load` always reaches through to it.
"""

from __future__ import annotations

from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority
from jarvis.mission.model import Mission, MissionState, Task
from jarvis.persistence.ports import MissionStore


class MissionEngine:
    def __init__(
        self,
        *,
        store: MissionStore,
        bus: EventBus,
        audit: AuditLogger | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._audit = audit
        self._cache: dict[str, Mission] = {}

    async def create(
        self,
        goal: str,
        *,
        correlation_id: str | None = None,
        device_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Mission:
        mission = Mission(goal=goal, device_id=device_id, context=context or {})
        if correlation_id is not None:
            mission.correlation_id = correlation_id
        await self._persist(mission)
        self._cache[mission.mission_id] = mission

        await self._bus.publish(
            Event(
                type=ev.MISSION_CREATED,
                source="mission-engine",
                correlation_id=mission.correlation_id,
                device_id=device_id,
                payload={
                    "mission_id": mission.mission_id,
                    "goal": goal,
                    "state": str(mission.state),
                },
            )
        )
        return mission

    async def transition(self, mission: Mission, target: MissionState, reason: str = "") -> Mission:
        """Apply a transition, persist it, then announce it."""
        previous = mission.state
        record = mission.transition(target, reason)

        await self._persist(mission)
        self._cache[mission.mission_id] = mission

        await self._bus.publish(
            Event(
                type=ev.MISSION_STATE_CHANGED,
                source="mission-engine",
                correlation_id=mission.correlation_id,
                device_id=mission.device_id,
                priority=(
                    Priority.URGENT
                    if target in (MissionState.FAILED, MissionState.BLOCKED)
                    else Priority.NORMAL
                ),
                payload={
                    "mission_id": mission.mission_id,
                    "from": str(previous),
                    "to": str(target),
                    "reason": reason,
                },
            )
        )
        if self._audit is not None:
            await self._audit.log(
                action="mission.transition",
                actor="mission-engine",
                subject=mission.mission_id,
                decision=str(target),
                correlation_id=mission.correlation_id,
                prev_state={"state": str(previous)},
                rollback_point={"state": str(previous), "at": record.at.isoformat()},
                goal=mission.goal,
                reason=reason,
            )
        return mission

    async def add_task(self, mission: Mission, task: Task) -> Task:
        mission.add_task(task)
        await self._persist(mission)
        await self._bus.publish(
            Event(
                type=ev.MISSION_TASK_STARTED,
                source="mission-engine",
                correlation_id=mission.correlation_id,
                device_id=mission.device_id,
                payload={
                    "mission_id": mission.mission_id,
                    "task_id": task.task_id,
                    "description": task.description,
                    "capability": task.capability,
                },
            )
        )
        return task

    async def save(self, mission: Mission) -> None:
        await self._persist(mission)
        self._cache[mission.mission_id] = mission

    async def load(self, mission_id: str) -> Mission | None:
        """Read a mission back from the store, bypassing the cache."""
        record = await self._store.load_mission(mission_id)
        if record is None:
            return None
        mission = Mission.from_dict(record)
        self._cache[mission_id] = mission
        return mission

    async def list(self, *, state: MissionState | None = None, limit: int = 100) -> list[Mission]:
        records = await self._store.list_missions(state=str(state) if state else None, limit=limit)
        return [Mission.from_dict(r) for r in records]

    async def resume_open_missions(self) -> list[Mission]:
        """Bring non-terminal missions back after a restart.

        A mission that was RUNNING when the process died is not running now, so
        it is moved to PAUSED - an honest state the owner can act on, rather
        than a stale RUNNING that would misreport live activity in the HUD.
        """
        resumed: list[Mission] = []
        for mission in await self.list():
            if mission.is_terminal:
                continue
            if mission.state is MissionState.RUNNING:
                await self.transition(
                    mission, MissionState.PAUSED, "process restart: run was interrupted"
                )
            self._cache[mission.mission_id] = mission
            resumed.append(mission)
        return resumed

    async def _persist(self, mission: Mission) -> None:
        await self._store.save_mission(mission.mission_id, str(mission.state), mission.to_dict())
