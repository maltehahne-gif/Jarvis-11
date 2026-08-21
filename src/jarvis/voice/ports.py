"""Voice engine ports - Blueprint 9, figure 4.

    Microphone -> Local Wake Word -> VAD + Turn Detection -> Streaming STT
      -> Fast Intent Router -> JARVIS Core / Claude -> Streaming TTS
      -> Best Speaker / Device

    "Wake, STT, Intent und TTS laufen nicht als serielle Blockkette."

Every box in that diagram that touches hardware or a model is a port here. Two
reasons, and the second is the one that shapes the interfaces:

* **Swappability.** Wake word, STT and TTS are exactly the components most
  likely to be replaced - a local model today, a better one next year. The
  Core must not care.
* **Streaming.** A serial chain would blow the latency budget in 9.2 no matter
  how fast each stage is, because the stages would have to finish before the
  next could start. So STT yields *partial* transcripts as it goes and TTS
  yields audio *per phrase*, which is what lets the intent router classify
  before the sentence is over and the speaker start before the answer is
  complete.

`speak()` returning an async iterator rather than a coroutine is what makes
barge-in possible at all: cancelling a generator between chunks stops audio
within one chunk, which is how Blueprint 9.2's "< 150 ms bis Audio stoppt" is
met.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jarvis.events.envelope import new_id, utc_now


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """The wake word was heard on a device."""

    device_id: str
    #: How sure the detector is. Blueprint 7.2 is explicit that voice identity
    #: is "Komfortsignal, kein alleiniger Authenticator" - this number may
    #: route audio, and may never authorise an action.
    confidence: float = 1.0
    phrase: str = "jarvis"
    wake_id: str = field(default_factory=new_id)
    at: Any = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wake_id": self.wake_id,
            "device_id": self.device_id,
            "confidence": self.confidence,
            "phrase": self.phrase,
            "at": self.at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Transcript:
    """One STT result. `final` marks the end of a turn."""

    text: str
    final: bool = False
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "final": self.final, "confidence": self.confidence}


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One piece of synthesised speech, ready for a speaker."""

    phrase: str
    #: Opaque to the Core; a real TTS returns encoded audio here.
    payload: bytes = b""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "bytes": len(self.payload),
            "duration_ms": self.duration_ms,
        }


@runtime_checkable
class WakeWordDetector(Protocol):
    """Local wake word. Blueprint 9.3 and Principle 4: never cloud-bound.

    A wake word that needed the network would make the very first moment of
    every interaction depend on it, which is precisely what "Fluid-first"
    forbids.
    """

    async def listen(self) -> AsyncIterator[WakeEvent]: ...


@runtime_checkable
class SpeechToText(Protocol):
    """Streaming transcription. Yields partials, then one final."""

    async def transcribe(self, device_id: str) -> AsyncIterator[Transcript]: ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Phrase-level synthesis.

    One chunk per phrase, not per sentence and not per word: Blueprint 9.1
    asks for "phrase-level streaming, natürliche Prosodie und dynamische
    Pausen" and forbids "abgehackte Satzstücke".
    """

    async def speak(
        self, phrases: tuple[str, ...], *, volume: float = 1.0
    ) -> AsyncIterator[AudioChunk]: ...


@runtime_checkable
class AudioSink(Protocol):
    """Where synthesised audio actually goes."""

    async def play(self, chunk: AudioChunk, *, device_id: str) -> None: ...

    async def stop(self, *, device_id: str) -> None: ...
