"""Intent Router - Blueprint 5.1 and DoD 5.4.

"Schnelle lokale Befehle von komplexen Reasoning-Aufgaben unterscheiden", and
as an exit criterion: "Intent Router wählt zwischen lokalem Mock-Tool und
Claude-Agent."

Routing is deterministic and synchronous. Nothing here awaits a model, because
this is the code path that decides whether a model is needed at all - asking one
first would defeat the purpose and break the latency budget in Blueprint 9.2
("Fast intent classification: typisch < 100 ms lokal/kleines Modell").

Control phrases are matched *before* everything else. "Jarvis, stop everything"
has to work when the network is down, when the provider is rate-limited, and
when an agent is mid-plan; Blueprint 7.2 requires the Core to stop agents
"unabhängig vom Modell", so the kill switch can never sit behind a model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.capability.registry import CapabilityRegistry
from jarvis.intent.rules import match_local


class Route(StrEnum):
    #: Dispatch straight to a capability through the Execution Gateway.
    LOCAL_TOOL = "local_tool"
    #: Hand to the Agent Coordinator for planning.
    AGENT = "agent"
    #: A Core control command (stop, resume). Never leaves the device.
    CONTROL = "control"


class ControlAction(StrEnum):
    STOP_EVERYTHING = "stop_everything"
    RESUME = "resume"


#: Blueprint 7.2's global kill switch, plus the phrasing to release it.
CONTROL_PATTERNS: tuple[tuple[re.Pattern[str], ControlAction], ...] = (
    (
        re.compile(
            r"\b(?:stop\s+everything|stopp?\s+alles|halt\s+alles|abbrechen|"
            r"notaus|emergency\s+stop)\b",
            re.IGNORECASE,
        ),
        ControlAction.STOP_EVERYTHING,
    ),
    (
        re.compile(
            r"\b(?:resume|weitermachen|fortsetzen|mach\s+weiter)\b",
            re.IGNORECASE,
        ),
        ControlAction.RESUME,
    ),
)


@dataclass(frozen=True, slots=True)
class Intent:
    """The router's decision about one incoming command."""

    route: Route
    text: str
    capability: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    control: ControlAction | None = None
    confidence: float = 0.0
    reason: str = ""

    @property
    def needs_model(self) -> bool:
        return self.route is Route.AGENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": str(self.route),
            "text": self.text,
            "capability": self.capability,
            "params": self.params,
            "control": str(self.control) if self.control else None,
            "confidence": self.confidence,
            "reason": self.reason,
            "needs_model": self.needs_model,
        }


class IntentRouter:
    """Deterministic front door for text commands."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def route(self, text: str) -> Intent:
        stripped = text.strip()

        # 1. Control first - must work with no model and no network.
        for pattern, action in CONTROL_PATTERNS:
            if pattern.search(stripped):
                return Intent(
                    route=Route.CONTROL,
                    text=stripped,
                    control=action,
                    confidence=1.0,
                    reason=f"control phrase matched: {action}",
                )

        # 2. A known local phrase dispatches straight to its capability.
        matched = match_local(stripped)
        if matched is not None:
            rule, params = matched
            if self._registry.has(rule.capability):
                return Intent(
                    route=Route.LOCAL_TOOL,
                    text=stripped,
                    capability=rule.capability,
                    params=params,
                    confidence=rule.confidence,
                    reason=rule.rationale,
                )

        # 3. Everything else is a reasoning problem.
        return Intent(
            route=Route.AGENT,
            text=stripped,
            confidence=0.5,
            reason="no local rule matched; needs planning",
        )
