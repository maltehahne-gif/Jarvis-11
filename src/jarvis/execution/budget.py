"""Execution budgets - Blueprint 7.3, "Agent-Endlosschleife".

Countermeasure named in the threat model: "time/token/cost budgets, watchdog,
checkpoints". A mission that loops must run out of budget rather than run
forever, and it must do so on a counter the model cannot talk its way past.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jarvis.events.envelope import utc_now


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard ceilings for one mission."""

    max_tool_calls: int = 50
    max_agent_calls: int = 20
    max_duration: timedelta = timedelta(minutes=10)
    max_cost_units: float = 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_agent_calls": self.max_agent_calls,
            "max_duration_seconds": self.max_duration.total_seconds(),
            "max_cost_units": self.max_cost_units,
        }


@dataclass(slots=True)
class BudgetUsage:
    """Live consumption against a `Budget`."""

    budget: Budget
    started_at: datetime = field(default_factory=utc_now)
    tool_calls: int = 0
    agent_calls: int = 0
    cost_units: float = 0.0

    def exceeded(self, now: datetime | None = None) -> str | None:
        """Return the name of the first exhausted limit, or `None`."""
        now = now or utc_now()
        if self.tool_calls >= self.budget.max_tool_calls:
            return "max_tool_calls"
        if self.agent_calls >= self.budget.max_agent_calls:
            return "max_agent_calls"
        if now - self.started_at >= self.budget.max_duration:
            return "max_duration"
        if self.cost_units >= self.budget.max_cost_units:
            return "max_cost_units"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "tool_calls": self.tool_calls,
            "agent_calls": self.agent_calls,
            "cost_units": self.cost_units,
            "budget": self.budget.to_dict(),
        }


class BudgetTracker:
    """Per-mission budget bookkeeping."""

    def __init__(self, default: Budget | None = None) -> None:
        self._default = default or Budget()
        self._usage: dict[str, BudgetUsage] = {}

    def start(self, mission_id: str, budget: Budget | None = None) -> BudgetUsage:
        usage = BudgetUsage(budget=budget or self._default)
        self._usage[mission_id] = usage
        return usage

    def usage(self, mission_id: str) -> BudgetUsage:
        if mission_id not in self._usage:
            self._usage[mission_id] = BudgetUsage(budget=self._default)
        return self._usage[mission_id]

    def exceeded(self, mission_id: str | None) -> str | None:
        if mission_id is None:
            return None
        return self.usage(mission_id).exceeded()

    def charge_tool_call(self, mission_id: str | None, cost: float = 0.0) -> None:
        if mission_id is None:
            return
        usage = self.usage(mission_id)
        usage.tool_calls += 1
        usage.cost_units += cost

    def charge_agent_call(self, mission_id: str | None, cost: float = 0.0) -> None:
        if mission_id is None:
            return
        usage = self.usage(mission_id)
        usage.agent_calls += 1
        usage.cost_units += cost

    def clear(self, mission_id: str) -> None:
        self._usage.pop(mission_id, None)
