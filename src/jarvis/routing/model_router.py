"""Model Router - Blueprint 5.1 and 6.1.

"Opus/Sonnet/lokal je nach Schwierigkeit, Latenz, Privatsphäre und Kosten."

The routing table is transcribed from Blueprint 6.1. Two rules sit *above* the
table and can only push a decision toward local execution:

* **Privacy.** A `SECRET` request is routed to a local model. Principle 3 keeps
  personal data at home, and no cost or quality argument overrides it.
* **Offline.** With no provider reachable, local rules must still drive the
  house and the PC - Blueprint 6.1's "Offline / private Basics" row.

Model identifiers live in one table so a future model generation is a data
change. Blueprint 6.1 is dated 19.08.2026 and explicitly warns against nailing
the architecture to any one model or plan detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jarvis.events.envelope import Sensitivity


class TaskClass(StrEnum):
    """The workload categories from Blueprint 6.1's table."""

    ARCHITECTURE = "architecture"
    HARD_REFACTOR = "hard_refactor"
    FEATURE = "feature"
    ROUTING = "routing"
    OFFLINE_BASIC = "offline_basic"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class ModelChoice:
    provider: str
    model: str
    effort: Effort
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": str(self.effort),
            "reason": self.reason,
        }


#: Blueprint 6.1, "Empfohlenes Modell/Setting" column.
DEFAULT_TABLE: dict[TaskClass, ModelChoice] = {
    TaskClass.ARCHITECTURE: ModelChoice(
        provider="claude",
        model="claude-opus-5",
        effort=Effort.MAX,
        reason="Architektur / Security / Datenmodell (Blueprint 6.1)",
    ),
    TaskClass.HARD_REFACTOR: ModelChoice(
        provider="claude",
        model="claude-opus-5",
        effort=Effort.XHIGH,
        reason="schwierige Refactorings / Multi-Agent (Blueprint 6.1)",
    ),
    TaskClass.FEATURE: ModelChoice(
        provider="claude",
        model="claude-sonnet-5",
        effort=Effort.HIGH,
        reason="normale Feature-Implementierung, besseres Kosten-/Latenz-Verhältnis",
    ),
    TaskClass.ROUTING: ModelChoice(
        provider="local",
        model="rules",
        effort=Effort.LOW,
        reason="Routing / Klassifikation braucht keine teure Frontier-KI",
    ),
    TaskClass.OFFLINE_BASIC: ModelChoice(
        provider="local",
        model="local-small",
        effort=Effort.LOW,
        reason="Home-/PC-Grundkommandos bleiben ohne Cloud funktionsfähig",
    ),
}

LOCAL_FALLBACK = ModelChoice(
    provider="local",
    model="local-small",
    effort=Effort.LOW,
    reason="local-only routing",
)


class ModelRouter:
    def __init__(self, table: dict[TaskClass, ModelChoice] | None = None) -> None:
        self._table = dict(table or DEFAULT_TABLE)

    def route(
        self,
        task_class: TaskClass,
        *,
        sensitivity: Sensitivity = Sensitivity.PRIVATE,
        offline: bool = False,
        latency_budget_ms: int | None = None,
    ) -> ModelChoice:
        if sensitivity is Sensitivity.SECRET:
            return ModelChoice(
                provider="local",
                model="local-small",
                effort=Effort.LOW,
                reason="secret sensitivity never leaves the device (Principle 3)",
            )
        if offline:
            return ModelChoice(
                provider="local",
                model="local-small",
                effort=Effort.LOW,
                reason="no provider reachable; local basics stay functional",
            )

        choice = self._table.get(task_class, self._table[TaskClass.FEATURE])

        # Fluid-first (Principle 4): a tight latency budget means a fast local
        # decision, never a slow high-effort one.
        if latency_budget_ms is not None and latency_budget_ms < 300:
            return ModelChoice(
                provider="local",
                model="rules",
                effort=Effort.LOW,
                reason=f"latency budget {latency_budget_ms}ms requires a local decision",
            )
        return choice

    def table(self) -> dict[str, dict[str, Any]]:
        return {str(k): v.to_dict() for k, v in self._table.items()}
