"""Privacy + Sensitivity Filter - Blueprint 8, figure 3, and 8.4.

In the memory pipeline this box sits between raw observations and *every*
store. Nothing becomes a belief without passing it, which is the only reason
the stores downstream can be treated as safe to read from.

It answers two questions about each observation:

* **May we remember this at all?** Privacy Mode, the "Don't Learn This" list,
  and the absolute refusal to store credential material all decide here.
* **How sensitive is it?** The assigned `Sensitivity` follows the entry for
  the rest of its life and is what stops a `SECRET` belief from being packed
  into a cloud-bound prompt by the Context Builder (Principle 3).

The filter is synchronous and side-effect free, for the same reason the
Permission Engine is: a privacy decision must be reproducible from its inputs
and reviewable without running the system.

Two defaults are deliberately conservative. Screen and camera learning is
**off** until the owner turns it on, because it is the most invasive source in
Blueprint 8.1 and the one it marks "nur nach Datenschutzregeln". And sensitivity
can only ever be raised by a caller, never lowered - the same
"overrides may only tighten" discipline the permission policy uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.events.envelope import Sensitivity
from jarvis.memory.models import MemoryType, Observation, Source
from jarvis.security.redaction import contains_credential

_SENSITIVITY_ORDER: dict[Sensitivity, int] = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PRIVATE: 1,
    Sensitivity.SECRET: 2,
}


class FilterRule(StrEnum):
    """Why the filter decided the way it did. Surfaced in "What JARVIS Knows"."""

    CREDENTIAL_MATERIAL = "credential_material"
    PRIVACY_MODE_BEHAVIOUR = "privacy_mode_behaviour"
    PRIVACY_MODE_CONVERSATION = "privacy_mode_conversation"
    PRIVACY_MODE_SCREEN_CAMERA = "privacy_mode_screen_camera"
    DONT_LEARN_THIS = "dont_learn_this"
    EMPTY_VALUE = "empty_value"
    ACCEPTED = "accepted"


@dataclass(slots=True)
class PrivacySettings:
    """The owner's standing privacy choices - Blueprint 8.4.

    "Privacy Mode: kein Verhalten lernen, optional kein Gesprächs-Memory,
    Screen/Kamera-Learning aus." Three independent switches, because the
    blueprint names them as three independent switches.
    """

    #: False = "kein Verhalten lernen": no habits, no preferences, no
    #: behavioural inference of any kind.
    learn_behaviour: bool = True
    #: False = "kein Gesprächs-Memory".
    conversation_memory: bool = True
    #: Off by default; the most invasive source in 8.1.
    screen_camera_learning: bool = False
    #: "Don't Learn This": (subject, predicate) pairs never to be stored.
    #: A `"*"` predicate blocks the whole subject.
    blocked: set[tuple[str, str]] = field(default_factory=set)

    @property
    def privacy_mode(self) -> bool:
        """True when any learning is currently suppressed."""
        return not self.learn_behaviour or not self.conversation_memory

    def block(self, subject: str, predicate: str = "*") -> None:
        self.blocked.add((subject, predicate))

    def unblock(self, subject: str, predicate: str = "*") -> None:
        self.blocked.discard((subject, predicate))

    def is_blocked(self, subject: str, predicate: str) -> bool:
        return (subject, predicate) in self.blocked or (subject, "*") in self.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "learn_behaviour": self.learn_behaviour,
            "conversation_memory": self.conversation_memory,
            "screen_camera_learning": self.screen_camera_learning,
            "privacy_mode": self.privacy_mode,
            "blocked": sorted(f"{s}:{p}" for s, p in self.blocked),
        }


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    """The filter's decision, with enough detail to explain a refusal."""

    accepted: bool
    rule: FilterRule
    reason: str
    sensitivity: Sensitivity = Sensitivity.PRIVATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rule": str(self.rule),
            "reason": self.reason,
            "sensitivity": str(self.sensitivity),
        }


#: Types that describe how the owner behaves rather than what is true of the
#: world. Blueprint 8.4's "kein Verhalten lernen" switch governs exactly these.
BEHAVIOURAL_TYPES: frozenset[MemoryType] = frozenset({MemoryType.PREFERENCE, MemoryType.HABIT})


class PrivacyFilter:
    def __init__(self, settings: PrivacySettings | None = None) -> None:
        self.settings = settings or PrivacySettings()

    def check(self, observation: Observation) -> FilterVerdict:
        """Decide whether this observation may become a belief, and how private."""
        s = self.settings

        # 1. Credential material is never stored, under any settings, from any
        #    source. Blueprint 7.2: the model should never see key material in
        #    clear text - and a memory store is a place it would see it again
        #    on every future context build.
        if contains_credential(observation.value) or contains_credential(observation.predicate):
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.CREDENTIAL_MATERIAL,
                reason="observation contains credential material and is never stored",
            )

        # 2. The owner's explicit "Don't Learn This".
        if s.is_blocked(observation.subject, observation.predicate):
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.DONT_LEARN_THIS,
                reason=f"{observation.subject}:{observation.predicate} is on the do-not-learn list",
            )

        # 3. Privacy Mode's three switches.
        if observation.from_screen_or_camera and not s.screen_camera_learning:
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.PRIVACY_MODE_SCREEN_CAMERA,
                reason="screen and camera learning is switched off",
            )
        if observation.from_conversation and not s.conversation_memory:
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.PRIVACY_MODE_CONVERSATION,
                reason="conversation memory is switched off",
            )
        if observation.type in BEHAVIOURAL_TYPES and not s.learn_behaviour:
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.PRIVACY_MODE_BEHAVIOUR,
                reason="behaviour learning is switched off",
            )

        # 4. Nothing useful to remember.
        if observation.value is None or observation.value == "":
            return FilterVerdict(
                accepted=False,
                rule=FilterRule.EMPTY_VALUE,
                reason="observation carries no value",
            )

        return FilterVerdict(
            accepted=True,
            rule=FilterRule.ACCEPTED,
            reason="accepted",
            sensitivity=self.classify(observation),
        )

    def classify(self, observation: Observation) -> Sensitivity:
        """Assign the sensitivity an observation carries for the rest of its life."""
        # Anything derived from watching a screen or a camera stays on the
        # device. Blueprint 8.1 admits visual memory only "nach
        # Datenschutzregeln"; this is that rule, made concrete.
        if observation.from_screen_or_camera or observation.type is MemoryType.VISUAL:
            return Sensitivity.SECRET
        # Facts about other people are not the owner's to send anywhere.
        if observation.type is MemoryType.RELATIONSHIP:
            return Sensitivity.SECRET
        return Sensitivity.PRIVATE


def raise_sensitivity(current: Sensitivity, requested: Sensitivity) -> Sensitivity:
    """Return whichever sensitivity is more restrictive.

    Callers may ask for *more* privacy than the filter assigned, never less -
    the same one-directional rule the permission policy uses for overrides, and
    for the same reason: a mistake here should cost convenience, not privacy.
    """
    return requested if _SENSITIVITY_ORDER[requested] > _SENSITIVITY_ORDER[current] else current


#: Sources whose observations are conversational by nature, kept here so the
#: service layer and the filter agree.
CONVERSATIONAL_SOURCES: frozenset[Source] = frozenset(
    {Source.EXPLICIT_STATEMENT, Source.CORRECTION}
)
