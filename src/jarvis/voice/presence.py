"""Presence and output routing - Blueprint 2.1, 9.1 and 10.5.

    "Der Core entscheidet, welches Display/Lautsprecher gerade der beste
    Ausgabepunkt ist. Andere Geräte bleiben still."  (2.1)

    "Ein globales Wake Event kann synchron an vertrauenswürdige Geräte
    gesendet werden, aber nur der beste Audio-Satellit antwortet, um
    Echo-Effekte zu vermeiden."  (10.5)

    "Night/Whisper/Silent Mode; sensitive Inhalte über Kopfhörer/Handy statt
    laut im Raum."  (9.1)

Two of those three are about convenience. The third is a privacy boundary, and
it is the one that decides this module's shape.

A room speaker is a broadcast device: whoever is in the room hears whatever it
says. The Privacy Filter already classifies screen captures, camera
observations and facts about other people as `SECRET`, and the Context Builder
already keeps `SECRET` memories out of cloud-bound prompts. Speaking one aloud
in a shared room would leak exactly what those two layers protect - through a
different exit. So `SECRET` output requires a private sink, and if there is no
private device online, JARVIS stays quiet and says so on a screen instead.

Choosing wrongly here is not symmetric. Picking a worse speaker costs audio
quality; picking a shared one costs the owner their privacy in front of
whoever is standing there. Every tie-break below leans the safe way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jarvis.events.envelope import Sensitivity
from jarvis.state.manager import DeviceState, StateManager
from jarvis.voice.personality import VoiceMode


class RoutingRule(StrEnum):
    """Why a device was chosen, or why none was. Surfaced for the HUD."""

    PRESENCE = "presence"
    BEST_PRIVATE = "best_private"
    BEST_SPEAKER = "best_speaker"
    NO_SPEAKER_ONLINE = "no_speaker_online"
    #: The only speakers available are shared, and the content is not.
    SENSITIVE_NEEDS_PRIVATE = "sensitive_needs_private"
    SILENT_MODE = "silent_mode"


@dataclass(frozen=True, slots=True)
class OutputChoice:
    """Where one response should be delivered."""

    device_id: str | None
    rule: RoutingRule
    speak: bool
    reason: str
    #: Devices that heard the wake word but must stay quiet, to avoid the echo
    #: Blueprint 10.5 warns about.
    silent_devices: tuple[str, ...] = ()
    fallback_to_screen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "rule": str(self.rule),
            "speak": self.speak,
            "reason": self.reason,
            "silent_devices": list(self.silent_devices),
            "fallback_to_screen": self.fallback_to_screen,
        }


class PresenceService:
    """Decides which single device answers."""

    def __init__(self, state: StateManager) -> None:
        self._state = state

    # -- candidates ---------------------------------------------------------

    def speakers(self) -> list[DeviceState]:
        """Online, trusted devices that can actually make sound.

        Untrusted devices are excluded outright. A device the owner has not
        vouched for is not somewhere JARVIS should be talking, whatever its
        audio quality.
        """
        return [d for d in self._state.devices() if d.online and d.trusted and d.has_speaker]

    def displays(self) -> list[DeviceState]:
        return [d for d in self._state.devices() if d.online and d.trusted and d.has_display]

    # -- the decision -------------------------------------------------------

    def choose_output(
        self,
        *,
        sensitivity: Sensitivity = Sensitivity.PRIVATE,
        mode: VoiceMode = VoiceMode.NORMAL,
        heard_on: str | None = None,
    ) -> OutputChoice:
        """Pick the one device that answers.

        `heard_on` is where the wake word was detected; it wins ties, because
        the device that heard you is usually the one you are next to. It does
        not override the privacy rule.
        """
        if not mode.speaks:
            return OutputChoice(
                device_id=None,
                rule=RoutingRule.SILENT_MODE,
                speak=False,
                reason="silent mode: answering on screen only",
                fallback_to_screen=True,
            )

        candidates = self.speakers()
        if not candidates:
            return OutputChoice(
                device_id=None,
                rule=RoutingRule.NO_SPEAKER_ONLINE,
                speak=False,
                reason="no trusted speaker is online",
                fallback_to_screen=True,
            )

        needs_private = sensitivity is Sensitivity.SECRET or mode.prefers_private_output
        private = [d for d in candidates if d.private_audio]

        if sensitivity is Sensitivity.SECRET and not private:
            # The refusal that matters. Blueprint 9.1 sends sensitive content
            # to headphones or a phone; with neither available, saying it out
            # loud is not the fallback - staying quiet is.
            return OutputChoice(
                device_id=None,
                rule=RoutingRule.SENSITIVE_NEEDS_PRIVATE,
                speak=False,
                reason="sensitive content, and no private audio device is online",
                silent_devices=tuple(sorted(d.device_id for d in candidates)),
                fallback_to_screen=True,
            )

        pool = private if (needs_private and private) else candidates
        chosen = self._best(pool, heard_on=heard_on)
        rule = (
            RoutingRule.PRESENCE
            if chosen.device_id in (heard_on, self._state.snapshot().get("presence"))
            else RoutingRule.BEST_PRIVATE
            if chosen.private_audio
            else RoutingRule.BEST_SPEAKER
        )

        return OutputChoice(
            device_id=chosen.device_id,
            rule=rule,
            speak=True,
            reason=self._explain(chosen, needs_private=needs_private),
            # Everything else that could have spoken stays quiet: one answer,
            # no echo (Blueprint 10.5).
            silent_devices=tuple(
                sorted(d.device_id for d in candidates if d.device_id != chosen.device_id)
            ),
        )

    def _best(self, pool: list[DeviceState], *, heard_on: str | None) -> DeviceState:
        """Rank candidates. Where you are beats how good the speaker is."""
        presence = self._state.snapshot().get("presence")

        def rank(device: DeviceState) -> tuple[int, int, int, str]:
            return (
                1 if device.device_id == heard_on else 0,
                1 if device.device_id == presence else 0,
                device.audio_quality,
                # Alphabetical last, so the choice is stable rather than
                # dependent on dict ordering.
                device.device_id,
            )

        return max(pool, key=rank)

    @staticmethod
    def _explain(device: DeviceState, *, needs_private: bool) -> str:
        if needs_private and device.private_audio:
            return f"{device.device_id} is private audio"
        if device.room:
            return f"best speaker in {device.room}"
        return f"best available speaker ({device.device_id})"

    # -- wake fan-out -------------------------------------------------------

    def wake_targets(self) -> list[str]:
        """Trusted devices that may show the wake acknowledgement.

        Blueprint 2.1 wants immediate visual feedback, and 10.5 allows the wake
        event to reach several devices at once. Only the *audio* answer is
        restricted to one - a light coming on in two rooms is not an echo.
        """
        return sorted(d.device_id for d in self._state.devices() if d.online and d.trusted)

    def snapshot(self) -> dict[str, Any]:
        return {
            "presence": self._state.snapshot().get("presence"),
            "speakers": [d.device_id for d in self.speakers()],
            "private_speakers": [d.device_id for d in self.speakers() if d.private_audio],
            "displays": [d.device_id for d in self.displays()],
        }
