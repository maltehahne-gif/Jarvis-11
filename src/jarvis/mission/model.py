"""Mission model and state machine - Blueprint 5.3.

    CREATED -> PLANNING -> WAITING_FOR_APPROVAL -> RUNNING
          -> VERIFYING -> COMPLETED
          -> PAUSED / BLOCKED / FAILED / CANCELED

    Every transition emits an event and is persisted.

The legal transitions are a table, and `Mission.transition` refuses anything not
in it. An illegal jump is a bug in the caller, and a mission that can be shoved
from CREATED straight to COMPLETED is exactly the "falsches 'fertig'" failure
the blueprint's threat model warns about - so the state machine will not do it,
whoever asks.

Every mission carries its own transition history. That history is what makes a
restarted mission legible instead of merely present: it survives the process,
so `PAUSED` after a crash still explains how it got there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from jarvis.events.envelope import new_id, utc_now


class MissionState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL_STATES: frozenset[MissionState] = frozenset(
    {MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELED}
)

#: The blueprint's happy path, plus the interrupt states it lists. Anything not
#: named here is refused.
LEGAL_TRANSITIONS: dict[MissionState, frozenset[MissionState]] = {
    MissionState.CREATED: frozenset({MissionState.PLANNING, MissionState.CANCELED}),
    MissionState.PLANNING: frozenset(
        {
            MissionState.WAITING_FOR_APPROVAL,
            MissionState.RUNNING,
            MissionState.BLOCKED,
            MissionState.FAILED,
            MissionState.CANCELED,
        }
    ),
    MissionState.WAITING_FOR_APPROVAL: frozenset(
        {
            MissionState.RUNNING,
            MissionState.BLOCKED,
            MissionState.FAILED,
            MissionState.CANCELED,
        }
    ),
    MissionState.RUNNING: frozenset(
        {
            MissionState.VERIFYING,
            MissionState.WAITING_FOR_APPROVAL,
            MissionState.PAUSED,
            MissionState.BLOCKED,
            MissionState.FAILED,
            MissionState.CANCELED,
        }
    ),
    # Verification may send a mission back to RUNNING: "Ziel nicht erreicht"
    # is a reason to retry, not automatically a failure.
    MissionState.VERIFYING: frozenset(
        {
            MissionState.COMPLETED,
            MissionState.RUNNING,
            MissionState.BLOCKED,
            MissionState.FAILED,
            MissionState.CANCELED,
        }
    ),
    MissionState.PAUSED: frozenset(
        {MissionState.RUNNING, MissionState.CANCELED, MissionState.FAILED}
    ),
    MissionState.BLOCKED: frozenset(
        {
            MissionState.PLANNING,
            MissionState.RUNNING,
            MissionState.CANCELED,
            MissionState.FAILED,
        }
    ),
    MissionState.COMPLETED: frozenset(),
    MissionState.FAILED: frozenset(),
    MissionState.CANCELED: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a caller asks for a transition the machine does not allow."""


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(slots=True)
class Task:
    """One planned step. Capability-bound so it can be permission-checked."""

    description: str
    capability: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    state: TaskState = TaskState.PENDING
    depends_on: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    task_id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "capability": self.capability,
            "params": self.params,
            "state": str(self.state),
            "depends_on": list(self.depends_on),
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            task_id=data["task_id"],
            description=data["description"],
            capability=data.get("capability"),
            params=data.get("params", {}),
            state=TaskState(data.get("state", TaskState.PENDING)),
            depends_on=list(data.get("depends_on", [])),
            result=data.get("result"),
        )


@dataclass(slots=True)
class Transition:
    """One recorded state change, kept for the mission's own history."""

    from_state: MissionState | None
    to_state: MissionState
    reason: str = ""
    at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": str(self.from_state) if self.from_state else None,
            "to": str(self.to_state),
            "reason": self.reason,
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            from_state=MissionState(data["from"]) if data.get("from") else None,
            to_state=MissionState(data["to"]),
            reason=data.get("reason", ""),
            at=datetime.fromisoformat(data["at"]),
        )


@dataclass(slots=True)
class Mission:
    """A long-running goal, its plan, and its lifecycle."""

    goal: str
    mission_id: str = field(default_factory=new_id)
    correlation_id: str = field(default_factory=new_id)
    state: MissionState = MissionState.CREATED
    tasks: list[Task] = field(default_factory=list)
    device_id: str | None = None
    user_id: str = "local-owner"
    context: dict[str, Any] = field(default_factory=dict)
    history: list[Transition] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(Transition(None, self.state, "created"))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def can_transition_to(self, target: MissionState) -> bool:
        return target in LEGAL_TRANSITIONS[self.state]

    def transition(self, target: MissionState, reason: str = "") -> Transition:
        """Move to `target`, or raise `IllegalTransition`."""
        if not self.can_transition_to(target):
            allowed = ", ".join(sorted(str(s) for s in LEGAL_TRANSITIONS[self.state])) or "none"
            raise IllegalTransition(
                f"mission {self.mission_id}: {self.state} -> {target} is not allowed "
                f"(allowed: {allowed})"
            )
        record = Transition(self.state, target, reason)
        self.state = target
        self.updated_at = record.at
        self.history.append(record)
        return record

    def add_task(self, task: Task) -> Task:
        self.tasks.append(task)
        self.updated_at = utc_now()
        return task

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "correlation_id": self.correlation_id,
            "goal": self.goal,
            "state": str(self.state),
            "tasks": [t.to_dict() for t in self.tasks],
            "device_id": self.device_id,
            "user_id": self.user_id,
            "context": self.context,
            "history": [h.to_dict() for h in self.history],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            mission_id=data["mission_id"],
            correlation_id=data["correlation_id"],
            goal=data["goal"],
            state=MissionState(data["state"]),
            tasks=[Task.from_dict(t) for t in data.get("tasks", [])],
            device_id=data.get("device_id"),
            user_id=data.get("user_id", "local-owner"),
            context=data.get("context", {}),
            history=[Transition.from_dict(h) for h in data.get("history", [])],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
