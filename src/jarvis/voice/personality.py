"""Voice Personality Contract - Blueprint 9.1.

The blueprint states this as prose. Most of it is genuinely mechanical, and
the mechanical parts belong in code rather than in a prompt - a rule that
lives only in a system prompt is a rule the model may quietly drop on a bad
day, and "in Notfall, Trauer, Medizin, Security oder ernsten Konflikten Humor
automatisch auf 0" is not a preference to be negotiated.

So the division is: **we decide the limits, the model fills the space inside
them.**

* **Humour level** is computed here from the topic and handed to the provider.
  Generating wit is the model's job; deciding that this is not the moment is
  ours.
* **The No-Filler Rule** is applied here, after the fact. "Natürlich", "Sehr
  gerne" and "Absolut" are stripped whether or not the model was told to avoid
  them.
* **Phrase splitting** is here because it is what makes streaming TTS sound
  continuous instead of chopped - "keine abgehackten Satzstücke".
* **Length** follows the mode and the owner's learned preference.

One rule is a safety rule rather than a style one: `SILENT` mode and sensitive
content do not merely shorten the answer, they stop it being spoken at all.
See `presence.py` for where that is enforced against actual devices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jarvis.events.envelope import Sensitivity


class Topic(StrEnum):
    """The five situations Blueprint 9.1 names, plus the ordinary case.

    Humour goes to zero in all five. They are listed explicitly rather than
    inferred, because a system that guessed at "is this serious?" would
    eventually guess wrong in the one case where being wrong is unforgivable.
    """

    NORMAL = "normal"
    EMERGENCY = "emergency"
    GRIEF = "grief"
    MEDICAL = "medical"
    SECURITY = "security"
    CONFLICT = "conflict"

    @property
    def is_serious(self) -> bool:
        return self is not Topic.NORMAL


class VoiceMode(StrEnum):
    """Blueprint 9.1's "Night/Whisper/Silent Mode"."""

    NORMAL = "normal"
    NIGHT = "night"
    WHISPER = "whisper"
    SILENT = "silent"

    @property
    def speaks(self) -> bool:
        return self is not VoiceMode.SILENT

    @property
    def volume(self) -> float:
        return {
            VoiceMode.NORMAL: 1.0,
            VoiceMode.NIGHT: 0.5,
            VoiceMode.WHISPER: 0.25,
            VoiceMode.SILENT: 0.0,
        }[self]

    @property
    def prefers_private_output(self) -> bool:
        """Night and whisper imply someone else is around, or asleep."""
        return self in (VoiceMode.NIGHT, VoiceMode.WHISPER)


class Verbosity(StrEnum):
    """How much detail. Learned from the owner (Blueprint 8.1, preference)."""

    TERSE = "terse"
    NORMAL = "normal"
    DETAILED = "detailed"

    @property
    def max_chars(self) -> int:
        return {Verbosity.TERSE: 120, Verbosity.NORMAL: 400, Verbosity.DETAILED: 1200}[self]


class ResponseKind(StrEnum):
    """What sort of thing is being said.

    `SIMPLE_ACK` exists because of one sentence in 9.1: "Bei simplen Aktionen
    oft nur 'Erledigt.' oder ein Sound Cue." Confirming a light switch with a
    paragraph is the failure mode that rule is aimed at.
    """

    SIMPLE_ACK = "simple_ack"
    ANSWER = "answer"
    STATUS = "status"
    APPROVAL_REQUEST = "approval_request"
    ERROR = "error"


#: Openers the blueprint names, plus the closest variants. Stripped only at the
#: start of an utterance - "das ist natürlich möglich" is ordinary German and
#: must survive.
FILLERS: tuple[str, ...] = (
    "natürlich",
    "selbstverständlich",
    "sehr gerne",
    "gerne",
    "absolut",
    "aber sicher",
    "klar doch",
    "kein problem",
    "of course",
    "certainly",
    "absolutely",
    "sure thing",
    "i'd be happy to",
    "happy to help",
)

_FILLER_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(f) for f in FILLERS) + r")\s*[,.!:—-]*\s*",
    re.IGNORECASE,
)

#: What a simple confirmed action sounds like. Blueprint 9.1's own example.
SIMPLE_ACK_TEXT = "Erledigt."

#: Below this, a fragment is not worth emitting on its own - splitting there
#: is what produces the "abgehackte Satzstücke" 9.1 forbids.
MIN_PHRASE_CHARS = 25

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:—])\s+")


@dataclass(slots=True)
class SpeechContext:
    """Everything that shapes how something should be said."""

    topic: Topic = Topic.NORMAL
    mode: VoiceMode = VoiceMode.NORMAL
    verbosity: Verbosity = Verbosity.NORMAL
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    device_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": str(self.topic),
            "mode": str(self.mode),
            "verbosity": str(self.verbosity),
            "sensitivity": str(self.sensitivity),
            "device_id": self.device_id,
        }


@dataclass(frozen=True, slots=True)
class Utterance:
    """A response, shaped and ready for the speaker.

    `suppressed` records what the contract removed. A rule that silently
    rewrote the system's own words would be hard to trust and harder to debug.
    """

    phrases: tuple[str, ...]
    volume: float = 1.0
    spoken: bool = True
    sound_cue: str | None = None
    humour_level: float = 0.0
    suppressed: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def text(self) -> str:
        return " ".join(self.phrases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrases": list(self.phrases),
            "text": self.text,
            "volume": self.volume,
            "spoken": self.spoken,
            "sound_cue": self.sound_cue,
            "humour_level": self.humour_level,
            "suppressed": list(self.suppressed),
            "truncated": self.truncated,
        }


def strip_fillers(text: str) -> tuple[str, list[str]]:
    """Remove leading filler openers. Returns the text and what was removed.

    Applied repeatedly, because "Natürlich, sehr gerne!" is two of them.
    """
    removed: list[str] = []
    current = text.lstrip()
    while (match := _FILLER_RE.match(current)) is not None:
        removed.append(match.group(0).strip())
        remainder = current[match.end() :].lstrip()
        if not remainder:
            # The whole utterance was filler. Something must remain to say.
            break
        current = remainder
    if current and removed:
        current = current[0].upper() + current[1:]
    return current, removed


def split_phrases(text: str, *, min_chars: int = MIN_PHRASE_CHARS) -> tuple[str, ...]:
    """Break text into phrases a TTS can start speaking one at a time.

    Sentences always end a phrase. Inside a long sentence, clause boundaries
    end one only once enough has accumulated - splitting at every comma is
    exactly how speech comes out chopped.
    """
    if not text.strip():
        return ()

    phrases: list[str] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= min_chars * 2:
            phrases.append(sentence)
            continue

        buffer = ""
        for clause in _CLAUSE_SPLIT.split(sentence):
            buffer = f"{buffer} {clause}".strip() if buffer else clause
            if len(buffer) >= min_chars:
                phrases.append(buffer)
                buffer = ""
        if buffer:
            # Too short to stand alone: append to the previous phrase rather
            # than emit a fragment.
            if phrases:
                phrases[-1] = f"{phrases[-1]} {buffer}".strip()
            else:
                phrases.append(buffer)
    return tuple(phrases)


def humour_level(context: SpeechContext) -> float:
    """How much wit is appropriate. Zero in every serious situation.

    Blueprint 9.1: "Sarkasmus trocken und selten" in general, and "in Notfall,
    Trauer, Medizin, Security oder ernsten Konflikten Humor automatisch auf 0".
    The everyday level is deliberately low rather than zero - dryness is part
    of the character, and constant jokes are their own failure.
    """
    if context.topic.is_serious:
        return 0.0
    if context.mode in (VoiceMode.NIGHT, VoiceMode.WHISPER, VoiceMode.SILENT):
        return 0.1
    return 0.25


class PersonalityContract:
    """Applies Blueprint 9.1 to one response."""

    def __init__(self, *, min_phrase_chars: int = MIN_PHRASE_CHARS) -> None:
        self._min_phrase_chars = min_phrase_chars

    def shape(
        self,
        text: str,
        context: SpeechContext | None = None,
        *,
        kind: ResponseKind = ResponseKind.ANSWER,
    ) -> Utterance:
        ctx = context or SpeechContext()
        suppressed: list[str] = []

        # A simple confirmed action does not get a sentence. It gets
        # "Erledigt." and, in silent or whisper mode, a cue instead.
        if kind is ResponseKind.SIMPLE_ACK:
            return self._acknowledge(ctx, original=text)

        cleaned, removed = strip_fillers(text)
        suppressed.extend(removed)

        limit = self._char_limit(ctx, kind)
        truncated = len(cleaned) > limit
        if truncated:
            cleaned = self._truncate(cleaned, limit)

        return Utterance(
            phrases=split_phrases(cleaned, min_chars=self._min_phrase_chars),
            volume=ctx.mode.volume,
            spoken=ctx.mode.speaks,
            humour_level=humour_level(ctx),
            suppressed=tuple(suppressed),
            truncated=truncated,
        )

    def _acknowledge(self, ctx: SpeechContext, *, original: str) -> Utterance:
        if not ctx.mode.speaks:
            return Utterance(phrases=(), spoken=False, volume=0.0, sound_cue="ack")
        if ctx.mode is VoiceMode.WHISPER:
            return Utterance(
                phrases=(),
                spoken=True,
                volume=ctx.mode.volume,
                sound_cue="ack",
                humour_level=humour_level(ctx),
            )
        return Utterance(
            phrases=(SIMPLE_ACK_TEXT,),
            volume=ctx.mode.volume,
            spoken=True,
            humour_level=humour_level(ctx),
            suppressed=(original,) if original and original != SIMPLE_ACK_TEXT else (),
        )

    @staticmethod
    def _char_limit(ctx: SpeechContext, kind: ResponseKind) -> int:
        base = ctx.verbosity.max_chars
        if ctx.mode in (VoiceMode.NIGHT, VoiceMode.WHISPER):
            base = min(base, Verbosity.TERSE.max_chars)
        if kind is ResponseKind.APPROVAL_REQUEST:
            # An approval request must survive intact: the owner is being
            # asked to authorise something and needs to hear what.
            return max(base, Verbosity.NORMAL.max_chars)
        return base

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Cut at a sentence boundary if there is one, never mid-word."""
        if len(text) <= limit:
            return text
        window = text[:limit]
        for end in (". ", "! ", "? "):
            cut = window.rfind(end)
            if cut > limit // 3:
                return window[: cut + 1].strip()
        cut = window.rfind(" ")
        return (window[:cut] if cut > 0 else window).strip() + " …"
