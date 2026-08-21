"""Voice engine - Blueprint 9, plus the presence rules from 2.1 and 10.5.

The properties that matter, stated as properties:

* The acknowledgement fires before transcription finishes. Without that, the
  250 ms feedback target in 9.2 is unreachable no matter how fast the parts
  are - it is the one thing that proves the pipeline is not a serial chain.
* Barge-in stops audio within 150 ms, even mid-sentence.
* Sensitive content is never spoken aloud in a shared room. With no private
  speaker available, JARVIS stays quiet rather than falling back to the room.
* Humour goes to zero in every serious situation, and the filler rule is
  applied by us rather than requested of the model.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.events import types as ev
from jarvis.events.envelope import Sensitivity
from jarvis.voice.latency import STRETCH_MS, TARGETS_MS, Checkpoint, LatencyMonitor, LatencyTrace
from jarvis.voice.mock import (
    RecordingAudioSink,
    ScriptedSpeechToText,
    ScriptedTextToSpeech,
    ScriptedWakeWord,
)
from jarvis.voice.personality import (
    SIMPLE_ACK_TEXT,
    PersonalityContract,
    ResponseKind,
    SpeechContext,
    Topic,
    Verbosity,
    VoiceMode,
    humour_level,
    split_phrases,
    strip_fillers,
)
from jarvis.voice.presence import PresenceService, RoutingRule

# --------------------------------------------------------------------------
# Blueprint 9.1 - the Voice Personality Contract
# --------------------------------------------------------------------------


class TestNoFillerRule:
    """9.1: "nicht ständig 'Natürlich', 'Sehr gerne', 'Absolut'"."""

    @pytest.mark.parametrize(
        "text",
        [
            "Natürlich, das Licht ist an.",
            "Sehr gerne! Das Licht ist an.",
            "Absolut. Das Licht ist an.",
            "Of course, das Licht ist an.",
        ],
    )
    def test_leading_fillers_are_stripped(self, text):
        cleaned, removed = strip_fillers(text)
        assert cleaned.startswith("Das Licht")
        assert removed

    def test_stacked_fillers_are_all_stripped(self):
        cleaned, removed = strip_fillers("Natürlich, sehr gerne! Das Licht ist an.")
        assert cleaned == "Das Licht ist an."
        assert len(removed) == 2

    def test_a_filler_word_mid_sentence_survives(self):
        """ "Das ist natürlich möglich" is ordinary German, not a filler."""
        cleaned, removed = strip_fillers("Das ist natürlich möglich.")
        assert cleaned == "Das ist natürlich möglich."
        assert removed == []

    def test_an_utterance_that_is_only_filler_still_says_something(self):
        cleaned, _ = strip_fillers("Natürlich.")
        assert cleaned

    def test_what_was_removed_is_reported(self):
        utterance = PersonalityContract().shape("Sehr gerne. Der Build läuft.")
        assert utterance.suppressed
        assert "Der Build läuft." in utterance.text


class TestSimpleActions:
    """9.1: "Bei simplen Aktionen oft nur 'Erledigt.' oder ein Sound Cue"."""

    def test_a_simple_action_gets_one_word(self):
        utterance = PersonalityContract().shape(
            "Ich habe das Licht im Office eingeschaltet, wie gewünscht.",
            kind=ResponseKind.SIMPLE_ACK,
        )
        assert utterance.phrases == (SIMPLE_ACK_TEXT,)

    def test_whisper_mode_uses_a_sound_cue_instead(self):
        utterance = PersonalityContract().shape(
            "Erledigt",
            SpeechContext(mode=VoiceMode.WHISPER),
            kind=ResponseKind.SIMPLE_ACK,
        )
        assert utterance.sound_cue == "ack"
        assert utterance.phrases == ()

    def test_silent_mode_says_nothing_aloud(self):
        utterance = PersonalityContract().shape(
            "Erledigt", SpeechContext(mode=VoiceMode.SILENT), kind=ResponseKind.SIMPLE_ACK
        )
        assert not utterance.spoken


class TestHumourGating:
    """9.1: humour automatically to 0 in five named situations."""

    @pytest.mark.parametrize(
        "topic",
        [Topic.EMERGENCY, Topic.GRIEF, Topic.MEDICAL, Topic.SECURITY, Topic.CONFLICT],
    )
    def test_serious_topics_get_no_humour(self, topic):
        assert humour_level(SpeechContext(topic=topic)) == 0.0

    def test_ordinary_conversation_keeps_a_dry_baseline(self):
        """ "Sarkasmus trocken und selten" - low, not zero."""
        level = humour_level(SpeechContext(topic=Topic.NORMAL))
        assert 0.0 < level < 0.5

    def test_night_mode_dials_humour_down(self):
        assert humour_level(SpeechContext(mode=VoiceMode.NIGHT)) < humour_level(SpeechContext())

    def test_the_level_travels_with_the_utterance(self):
        utterance = PersonalityContract().shape(
            "Der Schlüssel wurde rotiert.", SpeechContext(topic=Topic.SECURITY)
        )
        assert utterance.humour_level == 0.0


class TestPhraseStreaming:
    """9.1: "phrase-level streaming", "keine abgehackten Satzstücke"."""

    def test_sentences_become_separate_phrases(self):
        phrases = split_phrases("Der Build läuft. Die Tests sind grün.")
        assert len(phrases) == 2

    def test_a_short_sentence_is_not_split_further(self):
        assert split_phrases("Licht ist an, alles gut.") == ("Licht ist an, alles gut.",)

    def test_a_long_sentence_splits_at_clauses(self):
        long = (
            "Der Build ist durchgelaufen, die Tests sind alle grün, "
            "und das Artefakt liegt im Ausgabeverzeichnis bereit."
        )
        phrases = split_phrases(long)
        assert len(phrases) > 1

    def test_no_phrase_is_a_chopped_fragment(self):
        long = (
            "Ich habe nachgesehen, kurz, und es sieht gut aus, wirklich, "
            "der gesamte Durchlauf war ohne jeden Fehler und ohne Warnung."
        )
        phrases = split_phrases(long)
        # A trailing scrap is appended to its predecessor rather than emitted.
        assert all(len(p) >= 10 for p in phrases), phrases

    def test_empty_text_produces_no_phrases(self):
        assert split_phrases("   ") == ()


class TestLengthAdapts:
    def test_terse_verbosity_truncates(self):
        text = "Ein sehr langer Satz. " * 20
        utterance = PersonalityContract().shape(text, SpeechContext(verbosity=Verbosity.TERSE))
        assert utterance.truncated
        assert len(utterance.text) <= Verbosity.TERSE.max_chars + 5

    def test_truncation_cuts_at_a_sentence_boundary(self):
        text = "Erster Satz hier drin. " + "Zweiter viel längerer Satz. " * 10
        utterance = PersonalityContract().shape(text, SpeechContext(verbosity=Verbosity.TERSE))
        assert utterance.text.endswith((".", "…"))

    def test_an_approval_request_is_never_squeezed_to_nothing(self):
        """The owner is being asked to authorise something and needs to hear
        what it is."""
        text = (
            "Bestätigung nötig: Ich soll Software installieren, "
            "das ist eine kritische Aktion und braucht deine Freigabe."
        )
        utterance = PersonalityContract().shape(
            text,
            SpeechContext(verbosity=Verbosity.TERSE),
            kind=ResponseKind.APPROVAL_REQUEST,
        )
        assert not utterance.truncated

    def test_night_mode_shortens_even_a_detailed_preference(self):
        text = "Ein langer Bericht. " * 40
        utterance = PersonalityContract().shape(
            text, SpeechContext(verbosity=Verbosity.DETAILED, mode=VoiceMode.NIGHT)
        )
        assert utterance.truncated

    def test_volume_follows_the_mode(self):
        assert PersonalityContract().shape("x", SpeechContext(mode=VoiceMode.WHISPER)).volume < 0.5


# --------------------------------------------------------------------------
# Blueprint 9.2 - the latency budget
# --------------------------------------------------------------------------


class TestLatencyBudget:
    def test_every_checkpoint_from_the_table_has_a_target(self):
        assert set(TARGETS_MS) == set(Checkpoint)

    def test_the_targets_match_the_blueprint(self):
        assert TARGETS_MS[Checkpoint.WAKE_ACK] == 250.0
        assert TARGETS_MS[Checkpoint.BARGE_IN_STOP] == 150.0
        assert TARGETS_MS[Checkpoint.LOCAL_ACTION_DISPATCH] == 300.0
        assert TARGETS_MS[Checkpoint.INTENT_CLASSIFICATION] == 100.0
        assert TARGETS_MS[Checkpoint.FIRST_TTS_AUDIO] == 1000.0
        assert round(TARGETS_MS[Checkpoint.HUD_FRAME], 1) == 16.7
        assert TARGETS_MS[Checkpoint.MISSION_STATUS_EVENT] == 3000.0

    def test_the_stretch_goal_is_the_lower_end_of_the_range(self):
        assert STRETCH_MS[Checkpoint.WAKE_ACK] == 150.0
        assert STRETCH_MS[Checkpoint.FIRST_TTS_AUDIO] == 500.0

    def test_a_miss_is_reported_not_raised(self):
        """ "Zielwerte, keine Garantie" - a slow moment is not a broken one."""
        trace = LatencyTrace()
        trace.record(Checkpoint.WAKE_ACK, 900.0)

        assert not trace.within_budget
        assert trace.misses[0].checkpoint is Checkpoint.WAKE_ACK

    def test_a_fast_turn_is_within_budget(self):
        trace = LatencyTrace()
        trace.record(Checkpoint.WAKE_ACK, 20.0)
        trace.record(Checkpoint.FIRST_TTS_AUDIO, 200.0)
        assert trace.within_budget

    def test_the_monitor_reports_medians_and_miss_rates(self):
        monitor = LatencyMonitor(window=10)
        for value in (10.0, 20.0, 900.0):
            trace = LatencyTrace()
            trace.record(Checkpoint.WAKE_ACK, value)
            monitor.add(trace)

        report = monitor.report()
        row = next(r for r in report["checkpoints"] if r["checkpoint"] == "wake_ack")
        assert row["samples"] == 3
        assert row["miss_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_the_window_bounds_memory(self):
        monitor = LatencyMonitor(window=3)
        for _ in range(10):
            monitor.add(LatencyTrace())
        assert monitor.report()["turns"] == 3


# --------------------------------------------------------------------------
# Blueprint 2.1, 9.1, 10.5 - which device answers
# --------------------------------------------------------------------------


class TestPresence:
    @pytest.fixture
    async def presence(self, core: JarvisCore) -> PresenceService:
        await core.state.register_device(
            "kitchen-speaker",
            kind="satellite",
            trusted=True,
            has_speaker=True,
            room="kitchen",
            audio_quality=70,
        )
        await core.state.register_device(
            "desk-01",
            kind="desktop",
            trusted=True,
            has_speaker=True,
            has_display=True,
            room="office",
            audio_quality=50,
        )
        await core.state.register_device(
            "phone",
            kind="mobile",
            trusted=True,
            has_speaker=True,
            private_audio=True,
            audio_quality=30,
        )
        return core.presence

    async def test_only_one_device_answers(self, presence: PresenceService):
        """10.5: "nur der beste Audio-Satellit antwortet, um Echo zu vermeiden"."""
        choice = presence.choose_output()
        assert choice.device_id is not None
        assert len(choice.silent_devices) == 2

    async def test_the_device_that_heard_you_wins_ties(self, presence: PresenceService):
        choice = presence.choose_output(heard_on="desk-01")
        assert choice.device_id == "desk-01"

    async def test_secret_content_goes_to_private_audio(self, presence: PresenceService):
        """9.1: "sensitive Inhalte über Kopfhörer/Handy statt laut im Raum"."""
        choice = presence.choose_output(sensitivity=Sensitivity.SECRET, heard_on="kitchen-speaker")
        assert choice.device_id == "phone"
        assert choice.speak

    async def test_secret_content_stays_unspoken_without_a_private_device(self, core: JarvisCore):
        """The refusal that matters: a room speaker is not a fallback."""
        await core.state.register_device(
            "kitchen-speaker", trusted=True, has_speaker=True, room="kitchen"
        )
        choice = core.presence.choose_output(sensitivity=Sensitivity.SECRET)

        assert not choice.speak
        assert choice.device_id is None
        assert choice.rule is RoutingRule.SENSITIVE_NEEDS_PRIVATE
        assert choice.fallback_to_screen

    async def test_untrusted_devices_never_speak(self, core: JarvisCore):
        await core.state.register_device(
            "guest-speaker", trusted=False, has_speaker=True, audio_quality=100
        )
        choice = core.presence.choose_output()
        assert choice.device_id is None
        assert choice.rule is RoutingRule.NO_SPEAKER_ONLINE

    async def test_silent_mode_speaks_nowhere(self, presence: PresenceService):
        choice = presence.choose_output(mode=VoiceMode.SILENT)
        assert not choice.speak
        assert choice.fallback_to_screen

    async def test_night_mode_prefers_private_audio(self, presence: PresenceService):
        choice = presence.choose_output(mode=VoiceMode.NIGHT, heard_on="kitchen-speaker")
        assert choice.device_id == "phone"

    async def test_the_wake_ack_may_reach_every_trusted_device(self, presence: PresenceService):
        """2.1 wants immediate visual feedback; a light in two rooms is no echo."""
        assert len(presence.wake_targets()) == 3

    async def test_the_choice_is_stable(self, presence: PresenceService):
        assert presence.choose_output().device_id == presence.choose_output().device_id


# --------------------------------------------------------------------------
# Blueprint 9, figure 4 - the streaming pipeline
# --------------------------------------------------------------------------


@pytest.fixture
async def voice_core(config: CoreConfig):
    """A core whose voice input is scripted and whose output is recorded."""
    wake = ScriptedWakeWord()
    stt = ScriptedSpeechToText()
    tts = ScriptedTextToSpeech()
    sink = RecordingAudioSink()

    core = JarvisCore(config, wake=wake, stt=stt, tts=tts, sink=sink)
    await core.start()
    await core.state.register_device(
        "desk-01",
        kind="desktop",
        trusted=True,
        has_speaker=True,
        has_display=True,
        room="office",
        audio_quality=50,
    )
    try:
        yield core, wake, stt, tts, sink
    finally:
        await core.stop()


class TestPipeline:
    async def test_a_spoken_command_reaches_the_core(self, voice_core):
        core, wake, stt, _, sink = voice_core
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        assert turn.transcript == "Licht im Office an"
        assert turn.command.mission_state == "COMPLETED"
        assert core.world.lights["office"] == "on"

    async def test_the_acknowledgement_fires_before_transcription_finishes(self, voice_core):
        """The one measurement that proves this is not a serial chain."""
        core, wake, stt, _, _ = voice_core
        stt._partial_delay = 0.02  # a slow speaker
        stt.enqueue("Licht im Office an bitte")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        ack = turn.trace.get(Checkpoint.WAKE_ACK)
        assert ack is not None
        assert ack.within_target
        # Four partials at 20 ms each means transcription took ~80 ms; the
        # acknowledgement did not wait for any of it.
        assert ack.elapsed_ms < 20.0

    async def test_intent_is_classified_from_a_partial(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        assert turn.partials
        assert turn.early_intent is not None
        classification = turn.trace.get(Checkpoint.INTENT_CLASSIFICATION)
        assert classification.within_target

    async def test_a_simple_action_is_acknowledged_briefly(self, voice_core):
        core, wake, stt, _, sink = voice_core
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))
        assert turn.spoken_phrases == [SIMPLE_ACK_TEXT]
        assert sink.phrases_on("desk-01") == [SIMPLE_ACK_TEXT]

    async def test_the_first_audio_arrives_within_budget(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))
        assert turn.trace.get(Checkpoint.FIRST_TTS_AUDIO).within_target

    async def test_a_local_action_dispatches_within_budget(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))
        dispatch = turn.trace.get(Checkpoint.LOCAL_ACTION_DISPATCH)
        assert dispatch is not None and dispatch.within_target

    async def test_the_wake_event_is_published(self, voice_core):
        core, wake, stt, _, _ = voice_core
        sub = core.bus.subscribe(ev.VOICE_WAKE, name="test")
        try:
            stt.enqueue("Licht im Office an")
            await core.voice.handle_wake(wake.fire("desk-01"))
            event = sub.queue.get_nowait()
            assert event.payload["device_id"] == "desk-01"
            assert "ack_devices" in event.payload
        finally:
            sub.close()

    async def test_a_turn_without_speech_ends_quietly(self, voice_core):
        core, wake, _, _, sink = voice_core
        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        assert turn.transcript == ""
        assert sink.played == []

    async def test_the_listen_loop_survives_a_bad_turn(self, voice_core):
        core, wake, stt, _, _ = voice_core
        await core.voice.start()
        try:
            wake.fire("desk-01")  # nothing queued in STT
            stt.enqueue("Licht im Office an")
            wake.fire("desk-01")
            await asyncio.sleep(0.15)
            assert len(core.voice.turns) >= 2
        finally:
            await core.voice.stop()


class TestBargeIn:
    """9.1: "Jarvis, stopp" oder neue Frage beendet die laufende Sprachausgabe
    sofort." 9.2 puts that at under 150 ms."""

    async def test_speech_stops_within_the_budget(self, voice_core):
        core, wake, stt, tts, sink = voice_core
        tts._chunk_delay = 0.05
        stt.enqueue("Was ist der Systemstatus")

        turn_task = asyncio.create_task(core.voice.handle_wake(wake.fire("desk-01")))
        await asyncio.sleep(0.06)
        elapsed = await core.voice.barge_in("owner interrupted")
        await turn_task

        assert elapsed < TARGETS_MS[Checkpoint.BARGE_IN_STOP]

    async def test_the_speaker_is_told_to_stop(self, voice_core):
        core, wake, stt, tts, sink = voice_core
        tts._chunk_delay = 0.05
        stt.enqueue("Was ist der Systemstatus")

        turn_task = asyncio.create_task(core.voice.handle_wake(wake.fire("desk-01")))
        await asyncio.sleep(0.06)
        await core.voice.barge_in()
        await turn_task

        assert "desk-01" in sink.stopped

    async def test_remaining_phrases_are_not_played(self, voice_core):
        core, wake, stt, tts, sink = voice_core
        tts._chunk_delay = 0.05
        stt.enqueue("Was ist der Systemstatus")

        turn_task = asyncio.create_task(core.voice.handle_wake(wake.fire("desk-01")))
        await asyncio.sleep(0.06)
        await core.voice.barge_in()
        turn = await turn_task

        assert turn.barged_in
        if turn.utterance is not None and len(turn.utterance.phrases) > 1:
            assert len(turn.spoken_phrases) < len(turn.utterance.phrases)

    async def test_barge_in_with_nothing_playing_is_harmless(self, voice_core):
        core, _, _, _, sink = voice_core
        assert await core.voice.barge_in() >= 0.0
        assert sink.stopped == []

    async def test_a_barge_in_event_is_published(self, voice_core):
        core, wake, stt, tts, _ = voice_core
        sub = core.bus.subscribe(ev.VOICE_BARGE_IN, name="test")
        try:
            tts._chunk_delay = 0.05
            stt.enqueue("Was ist der Systemstatus")
            turn_task = asyncio.create_task(core.voice.handle_wake(wake.fire("desk-01")))
            await asyncio.sleep(0.06)
            await core.voice.barge_in("owner interrupted")
            await turn_task
            assert not sub.queue.empty()
        finally:
            sub.close()


class TestSensitiveOutputInThePipeline:
    async def test_a_secret_answer_is_not_spoken_in_a_room(self, config: CoreConfig):
        """End to end: reading a secret must not come out of a shared speaker."""
        wake, stt = ScriptedWakeWord(), ScriptedSpeechToText()
        sink = RecordingAudioSink()
        core = JarvisCore(config, wake=wake, stt=stt, tts=ScriptedTextToSpeech(), sink=sink)
        await core.start()
        try:
            await core.state.register_device(
                "kitchen-speaker", trusted=True, has_speaker=True, room="kitchen"
            )
            stt.enqueue("zeig mir das passwort für wlan")
            turn = await core.voice.handle_wake(wake.fire("kitchen-speaker"))

            assert not turn.answered_aloud
            assert sink.played == []
            assert turn.output.rule is RoutingRule.SENSITIVE_NEEDS_PRIVATE
        finally:
            await core.stop()

    async def test_withholding_is_announced_so_it_is_not_silent_failure(self, config: CoreConfig):
        wake, stt = ScriptedWakeWord(), ScriptedSpeechToText()
        core = JarvisCore(config, wake=wake, stt=stt, sink=RecordingAudioSink())
        await core.start()
        sub = core.bus.subscribe(ev.VOICE_WITHHELD, name="test")
        try:
            await core.state.register_device(
                "kitchen-speaker", trusted=True, has_speaker=True, room="kitchen"
            )
            stt.enqueue("zeig mir das passwort für wlan")
            await core.voice.handle_wake(wake.fire("kitchen-speaker"))

            event = sub.queue.get_nowait()
            assert event.payload["shown_on_screen_instead"] is True
        finally:
            sub.close()
            await core.stop()

    async def test_security_topics_get_no_humour(self, config: CoreConfig):
        wake, stt = ScriptedWakeWord(), ScriptedSpeechToText()
        core = JarvisCore(config, wake=wake, stt=stt, sink=RecordingAudioSink())
        await core.start()
        try:
            await core.state.register_device(
                "phone", trusted=True, has_speaker=True, private_audio=True
            )
            stt.enqueue("zeig mir das passwort für wlan")
            turn = await core.voice.handle_wake(wake.fire("phone"))
            assert turn.utterance.humour_level == 0.0
        finally:
            await core.stop()

    async def test_silent_mode_speaks_nothing_at_all(self, voice_core):
        core, wake, stt, _, sink = voice_core
        core.voice.mode = VoiceMode.SILENT
        stt.enqueue("Licht im Office an")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        assert sink.played == []
        # The action still happened; only the speaking was suppressed.
        assert core.world.lights["office"] == "on"
        assert turn.command.mission_state == "COMPLETED"


class TestVoiceCarriesNoAuthority:
    """Blueprint 7.2: voice identity is a comfort signal, not an authenticator."""

    async def test_a_spoken_critical_action_still_needs_confirmation(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("installiere Docker")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))

        assert turn.command.mission_state in ("WAITING_FOR_APPROVAL", "BLOCKED")
        assert core.world.installed == set()

    async def test_a_forbidden_action_is_refused_by_voice_too(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("mach einen factory reset")

        turn = await core.voice.handle_wake(wake.fire("desk-01"))
        assert turn.command.mission_state in ("BLOCKED", "FAILED")

    async def test_low_wake_confidence_does_not_authorise_anything(self, voice_core):
        core, wake, stt, _, _ = voice_core
        stt.enqueue("installiere Docker")

        turn = await core.voice.handle_wake(wake.fire("desk-01", confidence=0.2))
        assert core.world.installed == set()
        assert turn.command.mission_state != "COMPLETED"


# --------------------------------------------------------------------------
# Over the API
# --------------------------------------------------------------------------


class TestVoiceApi:
    def test_a_wake_turn_over_http(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            client.post(
                "/devices",
                json={
                    "device_id": "desk-01",
                    "trusted": True,
                    "has_speaker": True,
                    "has_display": True,
                },
            )
            body = client.post(
                "/voice/wake", json={"text": "Licht im Office an", "device_id": "desk-01"}
            ).json()

            assert body["transcript"] == "Licht im Office an"
            assert body["spoken_phrases"] == [SIMPLE_ACK_TEXT]
            assert body["latency"]["within_budget"] is True

    def test_mode_can_be_changed(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            body = client.post("/voice/mode", json={"mode": "night", "verbosity": "terse"}).json()
            assert body["mode"] == "night"
            assert body["verbosity"] == "terse"

    def test_latency_report_is_served(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            client.post(
                "/devices", json={"device_id": "desk-01", "trusted": True, "has_speaker": True}
            )
            client.post("/voice/wake", json={"text": "Systemstatus", "device_id": "desk-01"})
            report = client.get("/voice/latency").json()
            assert report["turns"] >= 1
            assert report["targets_are_goals_not_guarantees"] is True

    def test_presence_is_served(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            client.post(
                "/devices",
                json={
                    "device_id": "phone",
                    "trusted": True,
                    "has_speaker": True,
                    "private_audio": True,
                },
            )
            body = client.get("/presence").json()
            assert body["private_speakers"] == ["phone"]
