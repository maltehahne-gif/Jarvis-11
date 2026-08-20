"""Agent Coordinator - Blueprint 5.1 and 6.3.

"Claude/Subagents starten, stoppen, begrenzen, Ergebnisse zusammenführen."

The loop implements Blueprint 6.3's security-sensitive chain literally:

    Coordinator -> Implementer -> Security Reviewer -> Permission Engine -> Executor

The provider is the implementer; the Permission Engine and its gates are the
security reviewer; the Execution Gateway is the executor. The coordinator never
executes anything itself - it hands every proposed call to the gateway, which
re-validates the schema and re-checks permission on model-authored input.

Stopping is a Core concern, not a model concern: the kill switch is consulted
at the top of every turn, so "Jarvis, stop everything" halts an agent that is
mid-plan and would otherwise keep proposing work (Blueprint 7.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis.agents.provider import AgentRequest, AgentResponse, IntelligenceProvider
from jarvis.capability.models import ExecutionContext
from jarvis.capability.registry import CapabilityRegistry
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, Sensitivity
from jarvis.execution.gateway import ExecutionGateway, ExecutionOutcome, ExecutionResult
from jarvis.permission.engine import PermissionEngine
from jarvis.routing.model_router import ModelRouter, TaskClass

log = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 6


@dataclass(slots=True)
class AgentRun:
    """The merged outcome of one coordinated agent run."""

    goal: str
    correlation_id: str
    mission_id: str | None
    turns: int = 0
    summaries: list[str] = field(default_factory=list)
    executions: list[ExecutionResult] = field(default_factory=list)
    stopped_reason: str = "completed"
    model: str = "rules"

    @property
    def awaiting_confirmation(self) -> bool:
        return any(e.outcome is ExecutionOutcome.AWAITING_CONFIRMATION for e in self.executions)

    @property
    def succeeded(self) -> bool:
        """True when work happened and nothing that ran failed verification."""
        return bool(self.executions) and all(e.succeeded for e in self.executions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "turns": self.turns,
            "model": self.model,
            "summaries": self.summaries,
            "executions": [e.to_dict() for e in self.executions],
            "stopped_reason": self.stopped_reason,
            "awaiting_confirmation": self.awaiting_confirmation,
            "succeeded": self.succeeded,
        }


class AgentCoordinator:
    def __init__(
        self,
        *,
        provider: IntelligenceProvider,
        gateway: ExecutionGateway,
        registry: CapabilityRegistry,
        permissions: PermissionEngine,
        bus: EventBus,
        router: ModelRouter | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._provider = provider
        self._gateway = gateway
        self._registry = registry
        self._permissions = permissions
        self._bus = bus
        self._router = router or ModelRouter()
        self._max_turns = max_turns

    async def run(
        self,
        goal: str,
        context: ExecutionContext,
        *,
        task_class: TaskClass = TaskClass.FEATURE,
        sensitivity: Sensitivity = Sensitivity.PRIVATE,
        offline: bool = True,
        extra_context: dict[str, Any] | None = None,
    ) -> AgentRun:
        choice = self._router.route(task_class, sensitivity=sensitivity, offline=offline)
        run = AgentRun(
            goal=goal,
            correlation_id=context.correlation_id,
            mission_id=context.mission_id,
            model=choice.model,
        )

        await self._bus.publish(
            Event(
                type=ev.AGENT_INVOKED,
                source="agent-coordinator",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                sensitivity=sensitivity,
                payload={
                    "goal": goal,
                    "mission_id": context.mission_id,
                    "routing": choice.to_dict(),
                },
            )
        )

        # Only descriptions cross to the provider - never handlers.
        catalogue = self._registry.to_dict()
        history: list[dict[str, Any]] = []

        for turn in range(1, self._max_turns + 1):
            if self._permissions.kill_switch_engaged:
                run.stopped_reason = "kill_switch"
                break

            exhausted = self._gateway.budgets.exceeded(context.mission_id)
            if exhausted is not None:
                run.stopped_reason = f"budget:{exhausted}"
                break

            request = AgentRequest(
                goal=goal,
                correlation_id=context.correlation_id,
                available_capabilities=catalogue,
                context=extra_context or {},
                mission_id=context.mission_id,
                sensitivity=sensitivity,
                history=list(history),
            )

            try:
                response: AgentResponse = await self._provider.plan(
                    request, model=choice.model, effort=str(choice.effort)
                )
            except Exception as exc:
                log.exception("provider %s failed", self._provider.name)
                run.stopped_reason = f"provider_error:{type(exc).__name__}"
                await self._bus.publish(
                    Event(
                        type=ev.AGENT_FAILED,
                        source="agent-coordinator",
                        correlation_id=context.correlation_id,
                        priority=Priority.URGENT,
                        payload={"error": str(exc), "mission_id": context.mission_id},
                    )
                )
                break

            run.turns = turn
            run.summaries.append(response.summary)
            self._gateway.budgets.charge_agent_call(context.mission_id, response.cost_units)

            for call in response.tool_calls:
                result = await self._gateway.execute(call.capability, call.params, context)
                run.executions.append(result)
                history.append(
                    {
                        "turn": turn,
                        "capability": call.capability,
                        "params": call.params,
                        "outcome": str(result.outcome),
                        "detail": result.detail,
                        "verification": (
                            result.verification.to_dict() if result.verification else None
                        ),
                    }
                )
                if result.outcome is ExecutionOutcome.AWAITING_CONFIRMATION:
                    run.stopped_reason = "awaiting_confirmation"

            if run.stopped_reason == "awaiting_confirmation":
                break
            if response.done or not response.tool_calls:
                break
        else:
            run.stopped_reason = "max_turns"

        await self._bus.publish(
            Event(
                type=ev.AGENT_COMPLETED,
                source="agent-coordinator",
                correlation_id=context.correlation_id,
                device_id=context.device_id,
                payload={
                    "mission_id": context.mission_id,
                    "turns": run.turns,
                    "stopped_reason": run.stopped_reason,
                    "succeeded": run.succeeded,
                },
            )
        )
        return run
