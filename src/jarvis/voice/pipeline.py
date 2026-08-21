"""Voice pipeline - Blueprint 9, figure 4.

    Microphone -> Local Wake Word -> VAD + Turn Detection -> Streaming STT
      -> Fast Intent Router -> JARVIS Core / Claude -> Streaming TTS
      -> Best Speaker / Device

    "Wake, STT, Intent und TTS laufen nicht als serielle Blockkette."

The ordering here is not an optimisation; it is the design. Three things
happen out of the obvious order, each for a stated reason:

1. **The acknowledgement fires first.** Before transcription, before routing,
   before anything. Blueprint 9.2 allows 250 ms for "wahrgenommenes Feedback",
   and no pipeline that waits for a transcript can meet that. The owner learns
   they were heard while they are still talking.

2. **Intent classification runs on partial transcripts.** The router is
   deterministic and local (Blueprint 6.1's "Routing / Klassifikation" row),
   so it costs almost nothing to run it early and know what kind of turn this
   is before the sentence ends.

3. **Speech starts before the answer is complete.** TTS consumes phrases one
   at a time, so the first phrase can be playing while the rest is still being
   shaped.

Barge-in falls out of (3). Because speaking is a task iterating a generator,
cancelling it stops audio at the next chunk boundary, which is how "< 150 ms
bis Audio stoppt" is met without polling anything.

Nothing here decides what may be *done*. The pipeline hands text to
`JarvisCore.handle_command` and gets a result back; permission, planning and
verification all happen where they already happen. Voice is an input surface,
and Blueprint 7.2 is explicit that voice identity is "Komfortsignal, kein
alleiniger Authenticator" - so a spoken command carries no more authority than
a typed one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, Sensitivity, new_id
from jarvis.intent.router import Intent, Route
from jarvis.voice.latency import Checkpoint, LatencyMonitor, LatencyTrace
from jarvis.voice.personality import (
    PersonalityContract,
    ResponseKind,
    SpeechContext,
    Topic,
    Utterance,
    Verbosity,
    VoiceMode,
)
from jarvis.voice.ports import AudioSink, SpeechToText, TextToSpeech, WakeEvent, WakeWordDetector
from jarvis.voice.presence import OutputChoice, PresenceService

if TYPE_CHECKING:
    from jarvis.core import CommandResult, JarvisCore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class VoiceTurn:
    """One wake-to-answer cycle, with its timings and what was said."""

    wake: WakeEvent
    trace: LatencyTrace
    partials: list[str] = field(default_factory=list)
    transcript: str = ""
    early_intent: Intent | None = None
    command: CommandResult | None = None
    utterance: Utterance | None = None
    output: OutputChoice | None = None
    spoken_phrases: list[str] = field(default_factory=list)
    barged_in: bool = False
    turn_id: str = field(default_factory=new_id)

    @property
    def answered_aloud(self) -> bool:
        return bool(self.spoken_phrases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "wake": self.wake.to_dict(),
            "partials": list(self.partials),
            "transcript": self.transcript,
            "early_intent": self.early_intent.to_dict() if self.early_intent else None,
            "command": self.command.to_dict() if self.command else None,
            "utterance": self.utterance.to_dict() if self.utterance else None,
            "output": self.output.to_dict() if self.output else None,
            "spoken_phrases": list(self.spoken_phrases),
            "barged_in": self.barged_in,
            "answered_aloud": self.answered_aloud,
            "latency": self.trace.to_dict(),
        }


#: Response kinds that map onto what the Core reported. A completed simple
#: mission gets "Erledigt." rather than a sentence (Blueprint 9.1).
def _kind_for(result: CommandResult) -> ResponseKind:
    if result.pending_approval is not None:
        return ResponseKind.APPROVAL_REQUEST
    if result.mission_state in ("FAILED", "BLOCKED"):
        return ResponseKind.ERROR
    if result.mission_state == "COMPLETED" and result.agent_run is None:
        return ResponseKind.SIMPLE_ACK
    return ResponseKind.ANSWER


class VoicePipeline:
    def __init__(
        self,
        *,
        core: JarvisCore,
        wake: WakeWordDetector,
        stt: SpeechToText,
        tts: TextToSpeech,
        sink: AudioSink,
        presence: PresenceService,
        bus: EventBus,
        contract: PersonalityContract | None = None,
        monitor: LatencyMonitor | None = None,
        mode: VoiceMode = VoiceMode.NORMAL,
        verbosity: Verbosity = Verbosity.NORMAL,
    ) -> None:
        self._core = core
        self._wake = wake
        self._stt = stt
        self._tts = tts
        self._sink = sink
        self._presence = presence
        self._bus = bus
        self._contract = contract or PersonalityContract()
        self.monitor = monitor or LatencyMonitor()
        self.mode = mode
        self.verbosity = verbosity

        self._listener: asyncio.Task[None] | None = None
        self._speaking: asyncio.Task[None] | None = None
        self._speaking_device: str | None = None
        self._current: VoiceTurn | None = None
        self.turns: list[VoiceTurn] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._listener is None:
            self._listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        await self._cancel_speech()
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None

    async def _listen(self) -> None:
        async for event in self._wake.listen():
            try:
                await self.handle_wake(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad turn must not take the microphone offline.
                log.exception("voice turn failed")

    # -- one turn -----------------------------------------------------------

    async def handle_wake(self, wake: WakeEvent) -> VoiceTurn:
        """Run one wake-to-answer cycle."""
        trace = LatencyTrace()
        turn = VoiceTurn(wake=wake, trace=trace)
        self._current = turn
        self.turns.append(turn)

        # A wake word while JARVIS is talking is a barge-in. Blueprint 9.1
        # counts "eine neue Frage" as one, not only the word "stopp".
        if self._speaking is not None:
            await self.barge_in("new utterance", trace=trace)

        # (1) Acknowledge immediately. Nothing above the 250 ms line may wait
        #     on transcription, routing, or a model.
        await self._acknowledge(wake, trace)

        # (2) Transcribe, classifying partials as they arrive.
        await self._transcribe(turn, trace)
        if not turn.transcript:
            self.monitor.add(trace)
            return turn

        # (3) Hand the text to the Core. Everything about what may happen is
        #     decided there, exactly as for a typed command.
        result = await self._dispatch(turn, trace)
        turn.command = result

        # (4) Shape and speak.
        await self._respond(turn, result, trace)

        self.monitor.add(trace)
        self._current = None
        return turn

    async def _acknowledge(self, wake: WakeEvent, trace: LatencyTrace) -> None:
        measurement = trace.mark(Checkpoint.WAKE_ACK)
        await self._bus.publish(
            Event(
                type=ev.VOICE_WAKE,
                source="voice-pipeline",
                device_id=wake.device_id,
                priority=Priority.URGENT,
                payload={
                    **wake.to_dict(),
                    # Every trusted screen may light up; only one speaker will
                    # answer (Blueprint 2.1 and 10.5).
                    "ack_devices": self._presence.wake_targets(),
                    "ack_ms": round(measurement.elapsed_ms, 2),
                },
            )
        )

    async def _transcribe(self, turn: VoiceTurn, trace: LatencyTrace) -> None:
        classified = False
        async for transcript in self._stt.transcribe(turn.wake.device_id):
            if transcript.final:
                turn.transcript = transcript.text
                break

            turn.partials.append(transcript.text)
            if not classified:
                # The router is local and deterministic, so running it on a
                # partial costs nothing and tells us the shape of the turn
                # before it ends (Blueprint 6.1, "Routing / Klassifikation").
                started = time.monotonic()
                turn.early_intent = self._core.router.route(transcript.text)
                trace.mark(Checkpoint.INTENT_CLASSIFICATION, since=started)
                classified = True

        if turn.transcript:
            await self._bus.publish(
                Event(
                    type=ev.VOICE_TRANSCRIPT,
                    source="voice-pipeline",
                    device_id=turn.wake.device_id,
                    payload={
                        "turn_id": turn.turn_id,
                        "text": turn.transcript,
                        "partials": len(turn.partials),
                    },
                )
            )

    async def _dispatch(self, turn: VoiceTurn, trace: LatencyTrace) -> CommandResult:
        started = time.monotonic()
        result = await self._core.handle_command(turn.transcript, device_id=turn.wake.device_id)
        # Measured against what the Core actually routed, not against the early
        # guess: the partial-transcript classification is a latency
        # optimisation, and a measurement keyed to a guess would report on
        # turns that never happened. Only local actions are held to the 300 ms
        # target - a turn that needed a reasoner is not what that row of 9.2
        # measures.
        if result.intent is not None and result.intent.route in (
            Route.LOCAL_TOOL,
            Route.CONTROL,
        ):
            trace.mark(Checkpoint.LOCAL_ACTION_DISPATCH, since=started)
        return result

    # -- speaking -----------------------------------------------------------

    async def _respond(self, turn: VoiceTurn, result: CommandResult, trace: LatencyTrace) -> None:
        sensitivity = self._sensitivity_for(result)
        context = SpeechContext(
            topic=self._topic_for(result),
            mode=self.mode,
            verbosity=self.verbosity,
            sensitivity=sensitivity,
            device_id=turn.wake.device_id,
        )
        turn.utterance = self._contract.shape(result.message, context, kind=_kind_for(result))

        turn.output = self._presence.choose_output(
            sensitivity=sensitivity, mode=self.mode, heard_on=turn.wake.device_id
        )

        if not turn.output.speak or not turn.utterance.spoken or turn.output.device_id is None:
            await self._bus.publish(
                Event(
                    type=ev.VOICE_WITHHELD,
                    source="voice-pipeline",
                    device_id=turn.wake.device_id,
                    sensitivity=sensitivity,
                    priority=Priority.URGENT,
                    payload={
                        "turn_id": turn.turn_id,
                        "reason": turn.output.reason,
                        "rule": str(turn.output.rule),
                        "text": result.message,
                        "shown_on_screen_instead": turn.output.fallback_to_screen,
                    },
                )
            )
            return

        self._speaking_device = turn.output.device_id
        self._speaking = asyncio.create_task(self._speak(turn, trace))
        with contextlib.suppress(asyncio.CancelledError):
            await self._speaking
        self._speaking = None
        self._speaking_device = None

    async def _speak(self, turn: VoiceTurn, trace: LatencyTrace) -> None:
        assert turn.utterance is not None and turn.output is not None
        device_id = turn.output.device_id
        assert device_id is not None

        started = time.monotonic()
        first = True

        await self._bus.publish(
            Event(
                type=ev.VOICE_SPEAKING_STARTED,
                source="voice-pipeline",
                device_id=device_id,
                payload={
                    "turn_id": turn.turn_id,
                    "phrases": len(turn.utterance.phrases),
                    "routing": turn.output.to_dict(),
                },
            )
        )

        try:
            async for chunk in self._tts.speak(
                turn.utterance.phrases, volume=turn.utterance.volume
            ):
                await self._sink.play(chunk, device_id=device_id)
                turn.spoken_phrases.append(chunk.phrase)
                if first:
                    trace.mark(Checkpoint.FIRST_TTS_AUDIO, since=started)
                    first = False
        finally:
            await self._bus.publish(
                Event(
                    type=ev.VOICE_SPEAKING_FINISHED,
                    source="voice-pipeline",
                    device_id=device_id,
                    payload={
                        "turn_id": turn.turn_id,
                        "spoken": len(turn.spoken_phrases),
                        "complete": len(turn.spoken_phrases) == len(turn.utterance.phrases),
                    },
                )
            )

    async def barge_in(
        self, reason: str = "owner interrupted", *, trace: LatencyTrace | None = None
    ) -> float:
        """Stop speech immediately. Blueprint 9.1 and 9.2's 150 ms target.

        Cancelling the speaking task interrupts it at its next await, which
        for a phrase-streaming generator is at most one chunk away. That is
        the whole reason `TextToSpeech.speak` is an iterator.
        """
        started = time.monotonic()
        device_id = self._speaking_device

        if self._current is not None:
            self._current.barged_in = True
        await self._cancel_speech()
        if device_id is not None:
            await self._sink.stop(device_id=device_id)

        elapsed = (time.monotonic() - started) * 1000.0
        target = trace or (self._current.trace if self._current else None)
        if target is not None:
            target.record(Checkpoint.BARGE_IN_STOP, elapsed)

        await self._bus.publish(
            Event(
                type=ev.VOICE_BARGE_IN,
                source="voice-pipeline",
                device_id=device_id,
                priority=Priority.URGENT,
                payload={
                    "reason": reason,
                    "device_id": device_id,
                    "stopped_in_ms": round(elapsed, 2),
                },
            )
        )
        return elapsed

    async def _cancel_speech(self) -> None:
        if self._speaking is None:
            return
        task, self._speaking = self._speaking, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._speaking_device = None

    # -- classification helpers --------------------------------------------

    @staticmethod
    def _sensitivity_for(result: CommandResult) -> Sensitivity:
        """How private the *answer* is.

        Anything touching secrets is secret regardless of what was asked, so
        the routing decision cannot be talked out of it by phrasing.
        """
        capability = result.execution.capability if result.execution else None
        if capability and capability.startswith("security."):
            return Sensitivity.SECRET
        if result.intent is not None and result.intent.capability:
            if result.intent.capability.startswith("security."):
                return Sensitivity.SECRET
        return Sensitivity.PRIVATE

    @staticmethod
    def _topic_for(result: CommandResult) -> Topic:
        """Pick the humour gate. Security work is never funny (Blueprint 9.1)."""
        capability = result.execution.capability if result.execution else None
        if capability and capability.startswith("security."):
            return Topic.SECURITY
        if result.pending_approval is not None:
            return Topic.SECURITY
        if result.mission_state in ("FAILED", "BLOCKED"):
            return Topic.CONFLICT
        return Topic.NORMAL

    # -- introspection ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "listening": self._listener is not None,
            "speaking": self._speaking is not None,
            "speaking_on": self._speaking_device,
            "mode": str(self.mode),
            "verbosity": str(self.verbosity),
            "turns": len(self.turns),
            "presence": self._presence.snapshot(),
            "latency": self.monitor.report(),
        }
