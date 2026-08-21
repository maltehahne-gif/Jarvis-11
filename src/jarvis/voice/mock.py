"""In-process voice components for development and tests.

There is no microphone here, and there will not be one on a build server
either. These fakes exist so the *pipeline* - the part that is real - can be
driven end to end and its timing assertions can mean something.

They are modelled on how the real components behave rather than on what is
convenient to fake:

* STT yields growing partial transcripts and then one final, because that is
  what lets the intent router classify before the sentence ends.
* TTS yields one chunk per phrase with a configurable delay, because barge-in
  latency is a property of how often a generator yields.

`ScriptedTextToSpeech(chunk_delay=...)` is the knob that makes barge-in
testable: a slow speaker is exactly the situation the 150 ms target is about.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from jarvis.voice.ports import AudioChunk, Transcript, WakeEvent


class ScriptedWakeWord:
    """A wake detector the test fires by hand. Implements `WakeWordDetector`."""

    def __init__(self, device_id: str = "desk-01") -> None:
        self._default_device = device_id
        self._queue: asyncio.Queue[WakeEvent] = asyncio.Queue()
        self.fired: list[WakeEvent] = []

    def fire(self, device_id: str | None = None, *, confidence: float = 0.95) -> WakeEvent:
        event = WakeEvent(device_id=device_id or self._default_device, confidence=confidence)
        self.fired.append(event)
        self._queue.put_nowait(event)
        return event

    async def listen(self) -> AsyncIterator[WakeEvent]:
        while True:
            yield await self._queue.get()


class ScriptedSpeechToText:
    """Turns queued strings into partial-then-final transcripts.

    Implements `SpeechToText`.
    """

    def __init__(self, utterances: Sequence[str] = (), *, partial_delay: float = 0.0) -> None:
        self._queue: deque[str] = deque(utterances)
        self._partial_delay = partial_delay
        self.heard: list[str] = []

    def enqueue(self, text: str) -> None:
        self._queue.append(text)

    async def transcribe(self, device_id: str) -> AsyncIterator[Transcript]:
        if not self._queue:
            return
        text = self._queue.popleft()
        self.heard.append(text)

        words = text.split()
        # Partials grow a word at a time, as a real recogniser's do. The last
        # word is not emitted as a partial - it arrives with the final.
        for index in range(1, len(words)):
            if self._partial_delay:
                await asyncio.sleep(self._partial_delay)
            yield Transcript(text=" ".join(words[:index]), final=False, confidence=0.6)

        if self._partial_delay:
            await asyncio.sleep(self._partial_delay)
        yield Transcript(text=text, final=True, confidence=0.95)


class ScriptedTextToSpeech:
    """Emits one audio chunk per phrase. Implements `TextToSpeech`."""

    def __init__(self, *, chunk_delay: float = 0.0, ms_per_char: float = 40.0) -> None:
        self._chunk_delay = chunk_delay
        self._ms_per_char = ms_per_char
        self.spoken: list[str] = []

    async def speak(
        self, phrases: tuple[str, ...], *, volume: float = 1.0
    ) -> AsyncIterator[AudioChunk]:
        for phrase in phrases:
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            self.spoken.append(phrase)
            yield AudioChunk(
                phrase=phrase,
                payload=phrase.encode(),
                duration_ms=len(phrase) * self._ms_per_char,
            )


@dataclass
class RecordingAudioSink:
    """Records what would have been played. Implements `AudioSink`."""

    played: list[tuple[str, AudioChunk]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    async def play(self, chunk: AudioChunk, *, device_id: str) -> None:
        self.played.append((device_id, chunk))

    async def stop(self, *, device_id: str) -> None:
        self.stopped.append(device_id)

    @property
    def phrases(self) -> list[str]:
        return [chunk.phrase for _, chunk in self.played]

    def phrases_on(self, device_id: str) -> list[str]:
        return [c.phrase for d, c in self.played if d == device_id]
