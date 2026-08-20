"""Context Builder - Blueprint 5.1, and the right-hand end of figure 3.

    "Nur den relevanten Kontext aus Memory, App-Zustand, Projekt und Sensoren
    zusammenstellen."

The emphasis in that sentence is on *nur* and *relevanten*. Handing a model
everything JARVIS knows would be easier to write and worse in three separate
ways: it costs tokens and latency (Principle 4), it buries the relevant fact
among a hundred irrelevant ones, and it sends private life history to a cloud
provider that had no need for it (Principle 3).

So this module is mostly about what it refuses to include. Three filters, in
order of how badly getting them wrong would hurt:

1. **Destination.** A `SECRET` memory never enters a context bound for a cloud
   provider. This is the same rule the Model Router applies when it forces
   `SECRET` *requests* to a local model - applied here to the knowledge going
   into the request, not just the request itself. Screen captures, camera
   observations and facts about other people are classified `SECRET` by the
   Privacy Filter precisely so they land on the right side of this line.
2. **Confidence.** A belief below the personalisation threshold is a
   hypothesis, and Blueprint 8.3 says hypotheses may suggest, not steer. An
   unconfirmed guess presented to a model as established fact is how a wrong
   belief becomes self-reinforcing.
3. **Budget.** Relevance ranking is worthless if everything is included
   anyway.

Whatever is withheld is *counted* in the result. A context that silently
dropped half its input would make debugging a bad answer nearly impossible,
and the counts are cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.events.envelope import Sensitivity
from jarvis.memory.index import ScoredMemory
from jarvis.memory.models import PERSONALISATION_THRESHOLD, MemoryType
from jarvis.memory.service import MemoryService

#: Roughly four characters per token; a deliberately crude estimate, since the
#: only decision it drives is "stop adding entries".
DEFAULT_BUDGET_CHARS = 4000

DEFAULT_MEMORY_LIMIT = 12


class Destination(StrEnum):
    """Where the assembled context is about to travel."""

    LOCAL = "local"
    CLOUD = "cloud"

    @classmethod
    def for_provider(cls, provider: str) -> Destination:
        """Map a Model Router provider name onto a destination.

        Anything that is not demonstrably local is treated as cloud. Guessing
        wrong in that direction withholds context; guessing wrong in the other
        direction leaks it.
        """
        return cls.LOCAL if provider == "local" else cls.CLOUD


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The assembled context, plus an account of what was left out."""

    goal: str
    destination: Destination
    memories: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    project_scope: str | None = None
    withheld_secret: int = 0
    withheld_low_confidence: int = 0
    withheld_budget: int = 0
    used_chars: int = 0
    budget_chars: int = DEFAULT_BUDGET_CHARS

    @property
    def withheld_total(self) -> int:
        return self.withheld_secret + self.withheld_low_confidence + self.withheld_budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "destination": str(self.destination),
            "memories": self.memories,
            "state": self.state,
            "project_scope": self.project_scope,
            "withheld": {
                "secret": self.withheld_secret,
                "low_confidence": self.withheld_low_confidence,
                "budget": self.withheld_budget,
                "total": self.withheld_total,
            },
            "used_chars": self.used_chars,
            "budget_chars": self.budget_chars,
        }


def _summarise(scored: ScoredMemory) -> dict[str, Any]:
    """The shape a memory takes inside a context.

    Trimmed to what a reasoner can use: the claim, how sure we are, and where
    it came from. Internal ids and bookkeeping stay behind.
    """
    entry = scored.entry
    return {
        "type": str(entry.type),
        "subject": entry.subject,
        "predicate": entry.predicate,
        "value": entry.value,
        "confidence": entry.confidence,
        "source": str(entry.source),
        "last_confirmed_at": entry.last_confirmed_at.isoformat(),
    }


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryService,
        *,
        budget_chars: int = DEFAULT_BUDGET_CHARS,
        memory_limit: int = DEFAULT_MEMORY_LIMIT,
    ) -> None:
        self._memory = memory
        self._budget_chars = budget_chars
        self._memory_limit = memory_limit

    async def build(
        self,
        goal: str,
        *,
        destination: Destination = Destination.CLOUD,
        state: dict[str, Any] | None = None,
        project_scope: str | None = None,
        min_confidence: float = PERSONALISATION_THRESHOLD,
    ) -> BuiltContext:
        """Assemble the smallest context that could answer `goal`.

        Defaults are the safe ones: `CLOUD` destination and the personalisation
        threshold. A caller that wants more must ask for it deliberately.
        """
        # Retrieve generously, then filter hard. Ranking cannot know which
        # entries the destination will disqualify.
        candidates = await self._memory.recall(goal, limit=self._memory_limit * 3)

        withheld_secret = 0
        withheld_low_confidence = 0
        withheld_budget = 0
        used = 0
        selected: list[dict[str, Any]] = []

        for scored in candidates:
            entry = scored.entry

            if destination is Destination.CLOUD and entry.sensitivity is Sensitivity.SECRET:
                withheld_secret += 1
                continue

            if entry.confidence < min_confidence:
                withheld_low_confidence += 1
                continue

            summary = _summarise(scored)
            cost = len(str(summary))
            if used + cost > self._budget_chars or len(selected) >= self._memory_limit:
                withheld_budget += 1
                continue

            selected.append(summary)
            used += cost

        return BuiltContext(
            goal=goal,
            destination=destination,
            memories=selected,
            state=self._relevant_state(state),
            project_scope=project_scope,
            withheld_secret=withheld_secret,
            withheld_low_confidence=withheld_low_confidence,
            withheld_budget=withheld_budget,
            used_chars=used,
            budget_chars=self._budget_chars,
        )

    async def project_context(
        self, project: str, *, destination: Destination = Destination.CLOUD
    ) -> list[dict[str, Any]]:
        """Everything known about one project - Blueprint 8.1's Project memory.

        A traversal rather than a search: "what do we know about Atlas" has an
        exact answer, and the graph has it.
        """
        entries = await self._memory.graph.about(project)
        return [
            {
                "type": str(e.type),
                "predicate": e.predicate,
                "value": e.value,
                "confidence": e.confidence,
            }
            for e in entries
            if e.type in (MemoryType.PROJECT, MemoryType.SEMANTIC, MemoryType.PROCEDURAL)
            and not (destination is Destination.CLOUD and e.sensitivity is Sensitivity.SECRET)
            and e.confidence >= PERSONALISATION_THRESHOLD
        ]

    @staticmethod
    def _relevant_state(state: dict[str, Any] | None) -> dict[str, Any]:
        """Trim the State Manager snapshot to what a reasoner can act on.

        Device ids and trust flags stay out: they are the Permission Engine's
        business, and a model that knows which device is trusted is a model
        that can suggest using it.
        """
        if not state:
            return {}
        return {
            "active_missions": len(state.get("active_missions", [])),
            "presence": state.get("presence"),
            "devices_online": sum(1 for d in state.get("devices", []) if d.get("online")),
        }
