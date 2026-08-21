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


class TriggerKind(StrEnum):
    """What would set an approved routine off.

    Kept structured rather than parsed back out of the human-readable
    `trigger` sentence, because what can actually watch for a trigger differs
    per kind and a job that could never fire would be a broken promise.
    """

    #: Recurs, but not on a clock and not after any particular action. Usable
    #: by the Planner when the goal comes up again; nothing can watch for it.
    WHENEVER = "whenever"
    #: Recurs around a particular hour - Blueprint 8.1's "Tagesmuster".
    #: Watched by the Scheduler.
    DAILY = "daily"
    #: Follows another action. Watched by the Trigger Watcher, which fires it
    #: through the Scheduler when the preceding capability completes.
    AFTER = "after"


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
    trigger_kind: TriggerKind = TriggerKind.WHENEVER
    #: The hour bucket for DAILY, the preceding capability for AFTER.
    trigger_detail: str = ""
    status: ProposalStatus = ProposalStatus.PROPOSED
    proposal_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    decided_at: datetime | None = None
    source_memory_id: str | None = None
    #: Set once approval turned this into a scheduled job.
    job_id: str | None = None

    @property
    def schedulable(self) -> bool:
        """Whether something in the system can actually watch for this trigger.

        `WHENEVER` stays false: it names no moment, so there is nothing to
        wait on. Registering it would produce a job that never fires.
        """
        return self.trigger_kind in (TriggerKind.DAILY, TriggerKind.AFTER)

    def approved(self, *, job_id: str | None = None) -> Self:
        return replace(self, status=ProposalStatus.APPROVED, decided_at=utc_now(), job_id=job_id)

    def rejected(self) -> Self:
        return replace(self, status=ProposalStatus.REJECTED, decided_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "trigger": self.trigger,
            "trigger_kind": str(self.trigger_kind),
            "trigger_detail": self.trigger_detail,
            "schedulable": self.schedulable,
            "capability": self.capability,
            "params": self.params,
            "rationale": self.rationale,
            "observations": self.observations,
            "confidence": self.confidence,
            "status": str(self.status),
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "source_memory_id": self.source_memory_id,
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            proposal_id=data["proposal_id"],
            trigger=data["trigger"],
            trigger_kind=TriggerKind(data.get("trigger_kind", TriggerKind.WHENEVER)),
            trigger_detail=data.get("trigger_detail", ""),
            job_id=data.get("job_id"),
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


@dataclass(frozen=True, slots=True)
class _Described:
    trigger: str
    capability: str
    rationale: str
    kind: TriggerKind
    detail: str = ""


def _describe(entry: MemoryEntry) -> _Described | None:
    """Read a habit entry's predicate back into a trigger, if it is one."""
    predicate = entry.predicate
    if predicate.startswith("repeats:"):
        return _Described(
            trigger="whenever you would normally do it",
            capability=predicate.removeprefix("repeats:"),
            rationale=f"You have done this {entry.observations} times.",
            kind=TriggerKind.WHENEVER,
        )
    if predicate.startswith("time_pattern:"):
        return _Described(
            trigger=f"daily around {entry.value}",
            capability=predicate.removeprefix("time_pattern:"),
            rationale=f"You have done this around {entry.value} {entry.observations} times.",
            kind=TriggerKind.DAILY,
            detail=str(entry.value),
        )
    if predicate.startswith("follows:"):
        previous = predicate.removeprefix("follows:")
        return _Described(
            trigger=f"after {previous}",
            capability=str(entry.value),
            rationale=f"This followed {previous} {entry.observations} times.",
            kind=TriggerKind.AFTER,
            detail=previous,
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

    resolved_params = params
    if resolved_params is None and entry.predicate.startswith("repeats:"):
        try:
            resolved_params = json.loads(str(entry.value))
        except (ValueError, TypeError):
            resolved_params = {}

    return RoutineProposal(
        trigger=described.trigger,
        trigger_kind=described.kind,
        trigger_detail=described.detail,
        capability=described.capability,
        params=resolved_params or {},
        rationale=described.rationale,
        observations=entry.observations,
        confidence=entry.confidence,
        source_memory_id=entry.memory_id,
    )
