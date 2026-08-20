"""Intelligence Provider port - Blueprint 4.1 and 6.2.

"JARVIS ist die Kombination aus Core, State, Event Bus, Memory, Policy Engine,
Agent Runtime, Tool Registry, Device Mesh, Voice und UI. Claude liefert
Reasoning und agentische Planung." Keeping the model behind this port is what
makes Principle 1 ("Claude ist ein austauschbarer Intelligence Provider")
structural rather than aspirational.

One rule shapes the whole interface: **a provider proposes, the Core disposes.**
A provider returns `ProposedToolCall`s; it never receives a capability handler,
a secret, or a filesystem path it could act on. Execution goes through the
Execution Gateway, which re-validates the schema and re-runs the permission
check on whatever the model suggested. A model that hallucinates a capability
name or invents a parameter therefore fails a deterministic check instead of
reaching the OS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jarvis.events.envelope import Sensitivity


@dataclass(frozen=True, slots=True)
class ProposedToolCall:
    """A tool call the model *suggests*. Not yet permitted, not yet run."""

    capability: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "params": self.params,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Everything a provider is given - and nothing more.

    `available_capabilities` carries capability *descriptions* (name, schema,
    risk level), never callables. Secrets are absent by construction: Blueprint
    7.3 lists "keine secrets im prompt" as the countermeasure to prompt
    injection, so there is no field here that could carry one.
    """

    goal: str
    correlation_id: str
    available_capabilities: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    mission_id: str | None = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    #: Transcript of prior steps in this mission, oldest first.
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """What a provider returns for one turn of the agent loop."""

    summary: str
    tool_calls: tuple[ProposedToolCall, ...] = ()
    #: True when the provider believes the goal needs no further steps. The
    #: Verifier, not this flag, decides whether it was actually reached.
    done: bool = False
    cost_units: float = 0.0
    model: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "done": self.done,
            "cost_units": self.cost_units,
            "model": self.model,
        }


@runtime_checkable
class IntelligenceProvider(Protocol):
    """Reasoning and planning. Swappable: Claude, a local model, or rules."""

    name: str

    async def plan(self, request: AgentRequest, *, model: str, effort: str) -> AgentResponse: ...


@runtime_checkable
class AgentRuntime(Protocol):
    """A managed agent loop over an `IntelligenceProvider`.

    Blueprint 6.2 names the Claude Agent SDK as the first implementation. The
    port exists so that choice stays reversible.
    """

    async def run(self, request: AgentRequest) -> AgentResponse: ...

    async def cancel(self, correlation_id: str) -> None: ...
