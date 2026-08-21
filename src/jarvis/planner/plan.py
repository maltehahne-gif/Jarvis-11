"""Plan model - Blueprint 5.1 (Planner, Mission Engine) and 6.3 (Multi-Agent).

A plan is a directed acyclic graph of steps. The Mission Engine's job is to turn
"lang laufende Ziele in Tasks, Dependencies, Checkpoints und Status" - the
dependencies are what make it a graph rather than a list, and the graph is what
lets the runner know which steps are genuinely independent.

That last point is a blueprint rule, not an optimisation: 6.3 says "parallele
Agenten lohnen sich nur, wenn Teilprobleme wirklich unabhängig sind". `waves()`
answers exactly that question - steps in the same wave share no dependency path,
so they are the ones where parallelism is honest.

**Cycles are rejected at planning time.** A plan whose steps depend on each
other in a loop would not fail loudly at runtime; it would simply never become
ready, and the mission would sit in RUNNING until a budget expired. Since plans
can come from a model or from stored procedural memory, neither of which is
trusted to be well-formed, validation happens before a single step runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from jarvis.events.envelope import new_id
from jarvis.mission.model import Task
from jarvis.permission.levels import PermissionLevel


class AgentRole(StrEnum):
    """Who carries out a step - Blueprint 6.3's agent tree.

    `DIRECT` is the one that is not in the blueprint's diagram, and it matters
    most: a step whose capability and parameters are already known needs no
    reasoning at all. Routing it through a model would burn latency and tokens
    to re-derive something the Core already knows, which Principle 4 rules out.
    """

    DIRECT = "direct"
    COORDINATOR = "coordinator"
    RESEARCH = "research"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    VERIFICATION = "verification"
    SECURITY_REVIEW = "security_review"


class PlanError(ValueError):
    """Raised when a proposed plan is not executable as written."""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One node in the plan graph."""

    description: str
    capability: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    role: AgentRole = AgentRole.DIRECT
    #: Filled in by the Planner from the Capability Registry - never by whoever
    #: proposed the step.
    risk: PermissionLevel = PermissionLevel.P1_SAFE
    estimated_cost_units: float = 1.0
    step_id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "capability": self.capability,
            "params": self.params,
            "depends_on": list(self.depends_on),
            "role": str(self.role),
            "risk": self.risk.code,
            "estimated_cost_units": self.estimated_cost_units,
        }

    def to_task(self) -> Task:
        """The execution-time record for this step.

        Step ids carry over to task ids so a checkpoint written during
        execution still refers to something the plan can name.
        """
        return Task(
            task_id=self.step_id,
            description=self.description,
            capability=self.capability,
            params=dict(self.params),
            depends_on=list(self.depends_on),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated, risk-assessed graph of steps."""

    goal: str
    steps: tuple[PlanStep, ...]
    rationale: str = ""
    source: str = "planner"

    def __post_init__(self) -> None:
        self.validate()

    # -- structure ----------------------------------------------------------

    @property
    def step_ids(self) -> frozenset[str]:
        return frozenset(s.step_id for s in self.steps)

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def validate(self) -> None:
        """Reject anything that could not be executed as written."""
        if not self.steps:
            raise PlanError("a plan needs at least one step")

        ids = [s.step_id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise PlanError("duplicate step ids in plan")

        known = set(ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise PlanError(
                    f"step {step.step_id} depends on unknown step(s): {', '.join(sorted(unknown))}"
                )
            if step.step_id in step.depends_on:
                raise PlanError(f"step {step.step_id} depends on itself")

        self._reject_cycles()

    def _reject_cycles(self) -> None:
        """Kahn's algorithm; whatever it cannot order is part of a cycle."""
        remaining = {s.step_id: set(s.depends_on) for s in self.steps}
        ordered = 0
        while True:
            ready = [sid for sid, deps in remaining.items() if not deps]
            if not ready:
                break
            for sid in ready:
                del remaining[sid]
                ordered += 1
            for deps in remaining.values():
                deps.difference_update(ready)

        if remaining:
            raise PlanError(
                "plan contains a dependency cycle involving: " + ", ".join(sorted(remaining))
            )

    def waves(self) -> list[list[PlanStep]]:
        """Topological levels: steps in one wave depend on nothing in it.

        This is the structure 6.3's parallelism rule needs. It is also how the
        runner decides what is ready, so an empty return would be a bug rather
        than an empty plan - `validate` has already ruled that out.
        """
        by_id = {s.step_id: s for s in self.steps}
        remaining = {s.step_id: set(s.depends_on) for s in self.steps}
        levels: list[list[PlanStep]] = []

        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:  # pragma: no cover - validate() rejects cycles
                raise PlanError("plan contains a dependency cycle")
            levels.append([by_id[sid] for sid in ready])
            for sid in ready:
                del remaining[sid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return levels

    @property
    def is_parallelisable(self) -> bool:
        """True when at least one wave holds genuinely independent steps."""
        return any(len(wave) > 1 for wave in self.waves())

    # -- assessment ---------------------------------------------------------

    @property
    def max_risk(self) -> PermissionLevel:
        """The riskiest thing this plan would do.

        A plan is exactly as dangerous as its most dangerous step; averaging
        risk across steps would let one critical action hide behind nine safe
        ones.
        """
        return max((s.risk for s in self.steps), default=PermissionLevel.P0_OBSERVE)

    @property
    def estimated_cost_units(self) -> float:
        return sum(s.estimated_cost_units for s in self.steps)

    @property
    def tool_call_count(self) -> int:
        return sum(1 for s in self.steps if s.capability is not None)

    @property
    def agent_call_count(self) -> int:
        return sum(1 for s in self.steps if s.role is not AgentRole.DIRECT)

    def to_tasks(self) -> list[Task]:
        return [s.to_task() for s in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "rationale": self.rationale,
            "steps": [s.to_dict() for s in self.steps],
            "waves": [[s.step_id for s in wave] for wave in self.waves()],
            "max_risk": self.max_risk.code,
            "estimated_cost_units": self.estimated_cost_units,
            "tool_calls": self.tool_call_count,
            "agent_calls": self.agent_call_count,
            "parallelisable": self.is_parallelisable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            goal=data["goal"],
            steps=tuple(
                PlanStep(
                    step_id=s["step_id"],
                    description=s["description"],
                    capability=s.get("capability"),
                    params=s.get("params", {}),
                    depends_on=tuple(s.get("depends_on", [])),
                    role=AgentRole(s.get("role", AgentRole.DIRECT)),
                    risk=PermissionLevel(int(str(s.get("risk", "P1"))[1:])),
                    estimated_cost_units=s.get("estimated_cost_units", 1.0),
                )
                for s in data["steps"]
            ),
            rationale=data.get("rationale", ""),
            source=data.get("source", "planner"),
        )
