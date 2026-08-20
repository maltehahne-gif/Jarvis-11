"""Learning loop - Blueprint 8.3.

    Beobachtung -> Muster -> Hypothese -> Confidence -> sichere
    Personalisierung -> Feedback.

    "Wiederholte Verhaltensmuster dürfen zunächst Vorschläge erzeugen; erst
    nach Freigabe werden riskante oder weitreichende Automationen permanent."

That second sentence is the load-bearing one, and it is why this module
produces `RoutineProposal` objects rather than routines. A detected pattern is
a *suggestion the owner has not seen yet*. It does nothing, triggers nothing,
and changes no behaviour until someone approves it. This is also Blueprint
1.3's "keine unkontrollierte Selbstmodifikation" in practice: the system may
notice it repeats itself, but it may not decide on its own to start acting on
that.

Pattern detection reuses the confidence machinery from `models.py` rather than
keeping a second set of counters. A pattern *is* a habit memory: the entry's
`observations` count how often it recurred and its `confidence` is how sure we
are it is a real pattern rather than coincidence. When both cross their
thresholds, the pattern has earned the right to be *proposed*.

Blueprint 8.1 defines a habit as "wiederkehrende Sequenzen und Tagesmuster",
so all three detectors below are named there: plain repetition, time-of-day
patterns, and one action following another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from jarvis.events.envelope import new_id, utc_now
from jarvis.memory.models import (
    PERSONALISATION_THRESHOLD,
    MemoryEntry,
    MemoryType,
    Observation,
    Source,
)

#: How many times something must recur before it is even a candidate. Two is
#: coincidence; the third time is a pattern worth mentioning.
MIN_OBSERVATIONS_FOR_PROPOSAL = 3

OWNER = "owner"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RoutineProposal:
    """A pattern JARVIS noticed and would like permission to act on.

    Inert by construction. Nothing in the Core reads an unapproved proposal as
    a reason to do anything - it exists to be shown to the owner in "What
    JARVIS Knows" and accepted or declined there.
    """

    trigger: str
    capability: str
    params: dict[str, Any]
    rationale: str
    observations: int
    confidence: float
    status: ProposalStatus = ProposalStatus.PROPOSED
    proposal_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    decided_at: datetime | None = None
    source_memory_id: str | None = None

    def approved(self) -> Self:
        return replace(self, status=ProposalStatus.APPROVED, decided_at=utc_now())

    def rejected(self) -> Self:
        return replace(self, status=ProposalStatus.REJECTED, decided_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "trigger": self.trigger,
            "capability": self.capability,
            "params": self.params,
            "rationale": self.rationale,
            "observations": self.observations,
            "confidence": self.confidence,
            "status": str(self.status),
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "source_memory_id": self.source_memory_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            proposal_id=data["proposal_id"],
            trigger=data["trigger"],
            capability=data["capability"],
            params=data.get("params", {}),
            rationale=data["rationale"],
            observations=data["observations"],
            confidence=data["confidence"],
            status=ProposalStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            decided_at=(
                datetime.fromisoformat(data["decided_at"]) if data.get("decided_at") else None
            ),
            source_memory_id=data.get("source_memory_id"),
        )


def params_signature(params: dict[str, Any]) -> str:
    """A stable identity for one particular way of calling a capability.

    "Licht im Office an" and "Licht im Schlafzimmer an" are different habits,
    so the parameters are part of what makes a pattern a pattern.
    """
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def time_bucket(at: datetime) -> str:
    """Hour-of-day bucket, used for "Tagesmuster" (Blueprint 8.1).

    An hour is coarse enough that "roughly after work" still matches across
    days, and fine enough to be a useful trigger.
    """
    return f"{at.hour:02d}h"


def repetition_observation(
    capability: str, params: dict[str, Any], *, correlation_id: str | None = None
) -> Observation:
    return Observation(
        type=MemoryType.HABIT,
        subject=OWNER,
        predicate=f"repeats:{capability}",
        value=params_signature(params),
        source=Source.OBSERVATION,
        correlation_id=correlation_id,
    )


def time_pattern_observation(
    capability: str, at: datetime, *, correlation_id: str | None = None
) -> Observation:
    return Observation(
        type=MemoryType.HABIT,
        subject=OWNER,
        predicate=f"time_pattern:{capability}",
        value=time_bucket(at),
        source=Source.OBSERVATION,
        correlation_id=correlation_id,
    )


def sequence_observation(
    previous: str, capability: str, *, correlation_id: str | None = None
) -> Observation:
    return Observation(
        type=MemoryType.HABIT,
        subject=OWNER,
        predicate=f"follows:{previous}",
        value=capability,
        source=Source.OBSERVATION,
        correlation_id=correlation_id,
    )


def observations_for_action(
    capability: str,
    params: dict[str, Any],
    *,
    at: datetime,
    previous_capability: str | None = None,
    correlation_id: str | None = None,
) -> list[Observation]:
    """The habit observations one completed action generates."""
    observations = [
        repetition_observation(capability, params, correlation_id=correlation_id),
        time_pattern_observation(capability, at, correlation_id=correlation_id),
    ]
    if previous_capability is not None and previous_capability != capability:
        observations.append(
            sequence_observation(previous_capability, capability, correlation_id=correlation_id)
        )
    return observations


def _describe(entry: MemoryEntry) -> tuple[str, str, str] | None:
    """Turn a habit entry into (trigger, capability, rationale), if it is one."""
    predicate = entry.predicate
    if predicate.startswith("repeats:"):
        capability = predicate.removeprefix("repeats:")
        return (
            "whenever you would normally do it",
            capability,
            f"You have done this {entry.observations} times.",
        )
    if predicate.startswith("time_pattern:"):
        capability = predicate.removeprefix("time_pattern:")
        return (
            f"daily around {entry.value}",
            capability,
            f"You have done this around {entry.value} {entry.observations} times.",
        )
    if predicate.startswith("follows:"):
        previous = predicate.removeprefix("follows:")
        return (
            f"after {previous}",
            str(entry.value),
            f"This followed {previous} {entry.observations} times.",
        )
    return None


def propose_from(
    entry: MemoryEntry, *, params: dict[str, Any] | None = None
) -> RoutineProposal | None:
    """Turn a matured habit entry into a proposal, or return `None`.

    Both gates must pass: enough repetitions to rule out coincidence, and
    enough confidence that the pattern has held up. A pattern that keeps being
    contradicted loses confidence and never reaches the owner as a suggestion.
    """
    if entry.type is not MemoryType.HABIT:
        return None
    if entry.observations < MIN_OBSERVATIONS_FOR_PROPOSAL:
        return None
    if entry.confidence < PERSONALISATION_THRESHOLD:
        return None

    described = _describe(entry)
    if described is None:
        return None
    trigger, capability, rationale = described

    resolved_params = params
    if resolved_params is None and entry.predicate.startswith("repeats:"):
        try:
            resolved_params = json.loads(str(entry.value))
        except (ValueError, TypeError):
            resolved_params = {}

    return RoutineProposal(
        trigger=trigger,
        capability=capability,
        params=resolved_params or {},
        rationale=rationale,
        observations=entry.observations,
        confidence=entry.confidence,
        source_memory_id=entry.memory_id,
    )
