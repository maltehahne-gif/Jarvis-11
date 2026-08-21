"""Planner - Blueprint 5.1.

    "Schritte und Agenten bestimmen; Budget und Risiken berücksichtigen."

Two halves, and only one of them is allowed to be clever.

**Determining steps** may come from anywhere: a routed intent, a procedure the
owner taught JARVIS (Blueprint 8.1's procedural memory - "So deployen wir
Projekt X"), or, when nothing else fits, delegation to a reasoning agent.

**Determining risk and budget** may not. Risk is looked up in the Capability
Registry, never taken from whoever proposed the step, because a proposer that
could label its own step "safe" would make the whole P0-P6 table decorative
(Principle 2). Budget is checked against the same tracker the Execution Gateway
enforces, so a plan that provably cannot finish is refused before it starts
rather than discovered half-done.

Plans built from memory are proposals like any other. A stored procedure
naming a forbidden capability produces a plan that the Permission Engine will
refuse step by step - memory can suggest work, it cannot authorise it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis.capability.registry import CapabilityRegistry
from jarvis.execution.budget import Budget
from jarvis.memory.models import MemoryType
from jarvis.memory.service import MemoryService
from jarvis.permission.levels import PermissionLevel
from jarvis.planner.plan import AgentRole, Plan, PlanError, PlanStep

log = logging.getLogger(__name__)

#: At or above this level a plan needs the owner before it starts, not merely
#: before its risky step. Blueprint 7.1 puts confirmation at P3.
APPROVAL_THRESHOLD = PermissionLevel.P3_SENSITIVE

#: Cost estimate for a step that hands off to a reasoning agent. Agent turns
#: are the expensive part; a direct tool call is close to free by comparison.
AGENT_STEP_COST = 5.0
DIRECT_STEP_COST = 1.0


class PlanSource:
    """Where a plan's steps came from. Recorded for the HUD and audit."""

    INTENT = "intent"
    PROCEDURAL_MEMORY = "procedural_memory"
    DELEGATED = "delegated"
    EXPLICIT = "explicit"


@dataclass(frozen=True, slots=True)
class PlannedMission:
    """A plan plus the Core's own verdict on whether it may proceed."""

    plan: Plan
    budget: Budget
    requires_approval: bool
    budget_problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fits_budget(self) -> bool:
        return not self.budget_problems

    @property
    def executable(self) -> bool:
        return self.fits_budget

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "requires_approval": self.requires_approval,
            "fits_budget": self.fits_budget,
            "budget_problems": list(self.budget_problems),
            "notes": list(self.notes),
        }


class Planner:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        memory: MemoryService | None = None,
        default_budget: Budget | None = None,
    ) -> None:
        self._registry = registry
        self._memory = memory
        self._default_budget = default_budget or Budget()

    # -- entry point --------------------------------------------------------

    async def plan(
        self,
        goal: str,
        *,
        capability: str | None = None,
        params: dict[str, Any] | None = None,
        budget: Budget | None = None,
        project_scope: str | None = None,
        allow_memory: bool = True,
    ) -> PlannedMission:
        """Produce an assessed plan for `goal`.

        Sources are tried cheapest-first: a routed capability needs no lookup,
        a known procedure needs one local query, and delegating to an agent is
        the fallback that costs a model call.
        """
        notes: list[str] = []

        steps: tuple[PlanStep, ...] | None = None
        source = PlanSource.DELEGATED

        if capability is not None:
            steps = self._steps_for_capability(capability, params or {})
            source = PlanSource.INTENT

        if steps is None and allow_memory and self._memory is not None:
            remembered = await self._steps_from_memory(goal, project_scope=project_scope)
            if remembered is not None:
                steps, note = remembered
                source = PlanSource.PROCEDURAL_MEMORY
                notes.append(note)

        if steps is None:
            steps = self._delegation_steps(goal)

        plan = Plan(
            goal=goal,
            steps=steps,
            source=source,
            rationale=self._rationale(source, steps),
        )
        return self.assess(plan, budget=budget, notes=tuple(notes))

    # -- assessment (deterministic, ours) -----------------------------------

    def assess(
        self,
        plan: Plan,
        *,
        budget: Budget | None = None,
        notes: tuple[str, ...] = (),
    ) -> PlannedMission:
        """Attach real risk levels and check the plan against its budget."""
        effective = budget or self._default_budget
        rated = Plan(
            goal=plan.goal,
            steps=tuple(self._rate(step) for step in plan.steps),
            source=plan.source,
            rationale=plan.rationale,
        )

        problems: list[str] = []
        if rated.tool_call_count > effective.max_tool_calls:
            problems.append(
                f"plan needs {rated.tool_call_count} tool calls, "
                f"budget allows {effective.max_tool_calls}"
            )
        if rated.agent_call_count > effective.max_agent_calls:
            problems.append(
                f"plan needs {rated.agent_call_count} agent calls, "
                f"budget allows {effective.max_agent_calls}"
            )
        if rated.estimated_cost_units > effective.max_cost_units:
            problems.append(
                f"plan is estimated at {rated.estimated_cost_units:.1f} cost units, "
                f"budget allows {effective.max_cost_units:.1f}"
            )

        return PlannedMission(
            plan=rated,
            budget=effective,
            requires_approval=rated.max_risk >= APPROVAL_THRESHOLD,
            budget_problems=tuple(problems),
            notes=notes,
        )

    def _rate(self, step: PlanStep) -> PlanStep:
        """Replace a step's declared risk with the registry's verdict."""
        from dataclasses import replace

        if step.capability is None:
            # A thinking step is not itself risky; the calls it proposes are
            # each checked by the Permission Engine when they are made.
            return replace(step, risk=PermissionLevel.P1_SAFE)

        if not self._registry.has(step.capability):
            raise PlanError(f"plan references unregistered capability: {step.capability}")
        return replace(step, risk=self._registry.get(step.capability).level)

    # -- step sources -------------------------------------------------------

    def _steps_for_capability(
        self, capability: str, params: dict[str, Any]
    ) -> tuple[PlanStep, ...]:
        if not self._registry.has(capability):
            raise PlanError(f"unregistered capability: {capability}")
        return (
            PlanStep(
                description=f"{capability}",
                capability=capability,
                params=params,
                role=AgentRole.DIRECT,
                estimated_cost_units=DIRECT_STEP_COST,
            ),
        )

    async def _steps_from_memory(
        self, goal: str, *, project_scope: str | None
    ) -> tuple[tuple[PlanStep, ...], str] | None:
        """Look for a procedure the owner already taught JARVIS.

        Only confident procedural memory qualifies. A half-formed hypothesis
        about how something is done is not a plan (Blueprint 8.3), and the
        threshold is the same one the Context Builder uses.
        """
        assert self._memory is not None
        hits = await self._memory.recall(goal, limit=5, project_scope=project_scope)
        for hit in hits:
            if hit.entry.type is not MemoryType.PROCEDURAL or not hit.entry.actionable:
                continue
            steps = self._procedure_to_steps(hit.entry.predicate, hit.entry.value)
            if steps:
                return steps, (
                    f"reused a known procedure ({hit.entry.predicate}, "
                    f"confidence {hit.entry.confidence:.2f})"
                )
        return None

    def _procedure_to_steps(self, predicate: str, value: Any) -> tuple[PlanStep, ...]:
        """Turn a stored procedure into steps, skipping anything malformed.

        Stored procedures are data, not code, and they may have been written by
        an older version of this system. Anything that does not parse is
        skipped rather than guessed at.
        """
        if isinstance(value, dict) and isinstance(value.get("steps"), list):
            steps: list[PlanStep] = []
            index: dict[int, str] = {}
            for position, raw in enumerate(value["steps"]):
                if not isinstance(raw, dict) or not raw.get("capability"):
                    continue
                step = PlanStep(
                    description=raw.get("description") or str(raw["capability"]),
                    capability=str(raw["capability"]),
                    params=raw.get("params") or {},
                    depends_on=tuple(index[i] for i in raw.get("after", []) if i in index),
                    role=AgentRole.DIRECT,
                    estimated_cost_units=DIRECT_STEP_COST,
                )
                index[position] = step.step_id
                steps.append(step)
            return tuple(steps)

        # The single-step shape written by an approved routine (Blueprint 8.3).
        if predicate.startswith("routine:") and isinstance(value, dict):
            capability = predicate.removeprefix("routine:")
            return (
                PlanStep(
                    description=f"routine: {capability}",
                    capability=capability,
                    params=value.get("params") or {},
                    role=AgentRole.DIRECT,
                    estimated_cost_units=DIRECT_STEP_COST,
                ),
            )
        return ()

    @staticmethod
    def _delegation_steps(goal: str) -> tuple[PlanStep, ...]:
        """The fallback: let a reasoning agent work the goal out.

        Blueprint 6.3 warns against starting ten agents without need, so the
        default is one implementation step. The coordinator's own loop already
        runs proposal, permission check and verification around it - the
        Writer -> Verifier -> Security/QA chain is the machinery, not extra
        plan nodes.
        """
        return (
            PlanStep(
                description=f"work out and carry out: {goal}",
                capability=None,
                role=AgentRole.IMPLEMENTATION,
                estimated_cost_units=AGENT_STEP_COST,
            ),
        )

    @staticmethod
    def _rationale(source: str, steps: tuple[PlanStep, ...]) -> str:
        if source == PlanSource.INTENT:
            return "the command maps directly onto one registered capability"
        if source == PlanSource.PROCEDURAL_MEMORY:
            return f"followed a known procedure of {len(steps)} step(s)"
        return "no known procedure matched; delegating to a reasoning agent"
