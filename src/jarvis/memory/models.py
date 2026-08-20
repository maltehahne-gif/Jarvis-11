"""Memory model - Blueprint 8.1 and 8.2.

An entry is a subject-predicate-value triple carrying its own provenance. That
shape is not decoration: Blueprint 1.2 requires memory "mit Quellen, Confidence
und Löschbarkeit", and 8.4 requires a control surface where the owner sees
"Quelle, Confidence, letzte Bestätigung" for every single thing JARVIS believes.
A bag of free text could not answer those questions; a triple with metadata can.

The triple is also what makes the knowledge graph in figure 3 a graph at all -
`subject` and `value` are its nodes, `predicate` its edges.

Confidence is earned, never asserted. `with_observation` implements the
"Beobachtung -> Muster -> Hypothese -> Confidence" progression from 8.3: a
single sighting is a hypothesis, repetition is evidence, and a correction is
worth more than either because the owner said it in so many words.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from jarvis.events.envelope import Sensitivity, new_id, utc_now


class MemoryType(StrEnum):
    """The eight types from Blueprint 8.1, plus the optional visual one."""

    #: "Wir debuggen gerade `voice_router.py`." Never persisted - see below.
    WORKING = "working"
    #: "Am 19.08. wurde Build 0.3.4 nach einem Audio-Deadlock repariert."
    EPISODIC = "episodic"
    #: "Projekt Atlas verwendet PostgreSQL."
    SEMANTIC = "semantic"
    #: Architektur, offene Tasks, Entscheidungen, Dateien, Deploy-Verfahren.
    PROJECT = "project"
    #: Antwortlänge, IDE, Designstil, bevorzugte Apps.
    PREFERENCE = "preference"
    #: Wiederkehrende Sequenzen und Tagesmuster.
    HABIT = "habit"
    #: Personen/Firmen/Projekte und Beziehungen.
    RELATIONSHIP = "relationship"
    #: "So deployen wir Projekt X."
    PROCEDURAL = "procedural"
    #: Relevante Screens/Layouts - "nur nach Datenschutzregeln" (8.1).
    VISUAL = "visual"


class Source(StrEnum):
    """Where a belief came from. Blueprint 8.2's `source` field, verbatim."""

    EXPLICIT_STATEMENT = "explicit_statement"
    OBSERVATION = "observation"
    CORRECTION = "correction"


class Retention(StrEnum):
    """How long an entry is allowed to live."""

    DURABLE = "durable"
    #: Blueprint 8.4's "Make Temporary" action. Carries an `expires_at`.
    TEMPORARY = "temporary"


#: How far one new sighting moves confidence toward certainty, by source.
#: A correction moves it furthest: Blueprint 8.3 calls corrections
#: "besonders wertvoll" and asks that they update the user model deliberately.
LEARNING_RATE: dict[Source, float] = {
    Source.OBSERVATION: 0.15,
    Source.EXPLICIT_STATEMENT: 0.60,
    Source.CORRECTION: 0.80,
}

#: Confidence assigned the very first time something is seen.
INITIAL_CONFIDENCE: dict[Source, float] = {
    Source.OBSERVATION: 0.30,
    Source.EXPLICIT_STATEMENT: 0.85,
    Source.CORRECTION: 0.90,
}

#: How much a contradicting sighting costs.
CONTRADICTION_PENALTY = 0.45

#: Below this, an entry is a hypothesis: it is stored and visible in "What
#: JARVIS Knows", but the Context Builder will not act on it. Blueprint 8.3:
#: patterns may "zunächst Vorschläge erzeugen", not silently steer behaviour.
PERSONALISATION_THRESHOLD = 0.65


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One thing JARVIS believes, and why.

    Frozen: an update produces a new entry rather than mutating a belief in
    place. The same discipline as `Event` - and it keeps "last_confirmed_at"
    honest, because every change has to go through a method that sets it.
    """

    type: MemoryType
    subject: str
    predicate: str
    value: Any
    confidence: float = 0.3
    source: Source = Source.OBSERVATION
    observations: int = 1
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    retention: Retention = Retention.DURABLE
    project_scope: str | None = None
    memory_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    last_confirmed_at: datetime = field(default_factory=utc_now)

    # -- beyond Blueprint 8.2, required by the 8.4 control surface ----------

    #: "Pin" protects an entry from decay and from bulk forget windows.
    pinned: bool = False
    #: Set when `retention` is TEMPORARY.
    expires_at: datetime | None = None
    #: Which command produced this belief. Needed for "Jarvis, vergiss die
    #: letzten 30 Minuten" to know what to remove.
    correlation_id: str | None = None

    @property
    def key(self) -> tuple[str, str, str | None]:
        """Identity of the *belief*, independent of its current value.

        Two entries with the same key are claims about the same thing, so a
        new sighting either confirms or contradicts the existing one.
        """
        return (self.subject, self.predicate, self.project_scope)

    @property
    def actionable(self) -> bool:
        """Whether the Context Builder may act on this belief."""
        return self.confidence >= PERSONALISATION_THRESHOLD

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or utc_now()) >= self.expires_at

    def with_observation(self, *, source: Source, value: Any = None) -> Self:
        """Fold a new sighting of the same belief into this entry.

        A matching value confirms and raises confidence; a different value
        contradicts. A contradiction from a weak source lowers confidence but
        keeps the old value - one stray observation should not overwrite a
        well-established preference. A correction always wins outright,
        because the owner said so.
        """
        confirms = value is None or value == self.value
        now = utc_now()

        if confirms:
            rate = LEARNING_RATE[source]
            confidence = self.confidence + (1.0 - self.confidence) * rate
            new_value = self.value
        elif source is Source.CORRECTION:
            confidence = INITIAL_CONFIDENCE[Source.CORRECTION]
            new_value = value
        else:
            confidence = self.confidence * (1.0 - CONTRADICTION_PENALTY)
            new_value = self.value
            # A contradicted belief that has fallen apart adopts the new value
            # rather than clinging to one nobody has confirmed in a while.
            if confidence < 0.2:
                confidence = INITIAL_CONFIDENCE[source]
                new_value = value

        return replace(
            self,
            value=new_value,
            confidence=min(round(confidence, 4), 0.99),
            observations=self.observations + 1,
            source=source if not confirms or source is not Source.OBSERVATION else self.source,
            last_confirmed_at=now,
        )

    def corrected_to(self, value: Any) -> Self:
        """Blueprint 8.4's "Correct" action."""
        return self.with_observation(source=Source.CORRECTION, value=value)

    def pinned_copy(self) -> Self:
        return replace(self, pinned=True)

    def made_temporary(self, ttl: timedelta) -> Self:
        """Blueprint 8.4's "Make Temporary" action."""
        return replace(self, retention=Retention.TEMPORARY, expires_at=utc_now() + ttl)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "type": str(self.type),
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "confidence": self.confidence,
            "source": str(self.source),
            "observations": self.observations,
            "created_at": self.created_at.isoformat(),
            "last_confirmed_at": self.last_confirmed_at.isoformat(),
            "sensitivity": str(self.sensitivity),
            "retention": str(self.retention),
            "project_scope": self.project_scope,
            "pinned": self.pinned,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "correlation_id": self.correlation_id,
            "actionable": self.actionable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            memory_id=data["memory_id"],
            type=MemoryType(data["type"]),
            subject=data["subject"],
            predicate=data["predicate"],
            value=data["value"],
            confidence=data["confidence"],
            source=Source(data["source"]),
            observations=data["observations"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_confirmed_at=datetime.fromisoformat(data["last_confirmed_at"]),
            sensitivity=Sensitivity(data["sensitivity"]),
            retention=Retention(data["retention"]),
            project_scope=data.get("project_scope"),
            pinned=data.get("pinned", False),
            expires_at=(
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            ),
            correlation_id=data.get("correlation_id"),
        )


def new_entry(
    *,
    type: MemoryType,
    subject: str,
    predicate: str,
    value: Any,
    source: Source = Source.OBSERVATION,
    sensitivity: Sensitivity = Sensitivity.PRIVATE,
    project_scope: str | None = None,
    correlation_id: str | None = None,
) -> MemoryEntry:
    """Create a first-sighting entry with the confidence its source earns."""
    return MemoryEntry(
        type=type,
        subject=subject,
        predicate=predicate,
        value=value,
        confidence=INITIAL_CONFIDENCE[source],
        source=source,
        sensitivity=sensitivity,
        project_scope=project_scope,
        correlation_id=correlation_id,
    )


@dataclass(frozen=True, slots=True)
class Observation:
    """Raw input to the memory pipeline, before the Privacy Filter sees it.

    This is the left-hand box of figure 3: "commands, corrections, project
    events". It is deliberately *not* a `MemoryEntry` - nothing becomes a
    belief until it has passed the filter.
    """

    type: MemoryType
    subject: str
    predicate: str
    value: Any
    source: Source = Source.OBSERVATION
    project_scope: str | None = None
    correlation_id: str | None = None
    #: Set for observations derived from screen capture or camera, which the
    #: owner can disable independently (Blueprint 8.4).
    from_screen_or_camera: bool = False
    #: Set for observations derived from conversation, which the owner can
    #: disable independently (Blueprint 8.4).
    from_conversation: bool = False
