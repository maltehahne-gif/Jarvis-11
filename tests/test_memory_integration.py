"""Memory wired into the running Core - Blueprint 5.1, 8.3, 8.4.

The unit tests in `test_memory.py` prove each stage of the pipeline in
isolation. These prove the wiring: that driving the Core the way an owner
does actually produces memories, that the learning loop only learns from work
that demonstrably succeeded, and that the whole thing survives a restart.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from jarvis.api.server import create_app
from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.memory.models import MemoryType
from jarvis.memory.privacy import PrivacySettings


class TestEventBusWiring:
    """Blueprint 5.1 names Memory among the Event Bus's consumers."""

    async def test_a_command_becomes_an_episodic_memory(self, core: JarvisCore):
        await core.handle_command("Licht im Office an")
        episodes = await core.memory.entries(type=MemoryType.EPISODIC)
        assert any(e.value == "Licht im Office an" for e in episodes)

    async def test_a_successful_action_teaches_a_habit(self, core: JarvisCore):
        await core.handle_command("Licht im Office an")
        habits = await core.memory.entries(type=MemoryType.HABIT)
        predicates = {h.predicate for h in habits}
        assert "repeats:home.set_light" in predicates
        assert "time_pattern:home.set_light" in predicates

    async def test_repeating_a_command_strengthens_the_habit(self, core: JarvisCore):
        for _ in range(3):
            await core.handle_command("Licht im Office an")
        habit = next(
            h
            for h in await core.memory.entries(type=MemoryType.HABIT)
            if h.predicate == "repeats:home.set_light"
        )
        assert habit.observations == 3

    async def test_different_rooms_are_different_habits(self, core: JarvisCore):
        await core.handle_command("Licht im Office an")
        await core.handle_command("Licht im Living an")
        habit = next(
            h
            for h in await core.memory.entries(type=MemoryType.HABIT)
            if h.predicate == "repeats:home.set_light"
        )
        # Same predicate, contradicting values: a second room is not more
        # evidence for the first one.
        assert habit.observations == 2
        assert habit.confidence < 0.5

    async def test_a_mission_outcome_is_remembered(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        episodes = await core.memory.entries(type=MemoryType.EPISODIC)
        outcome = next(e for e in episodes if e.subject == f"mission:{result.mission_id}")
        assert outcome.value["state"] == "COMPLETED"

    async def test_a_blocked_action_teaches_no_habit(self, core: JarvisCore):
        await core.handle_command("mach einen factory reset")
        habits = await core.memory.entries(type=MemoryType.HABIT)
        assert not any("factory_reset" in h.predicate for h in habits)

    async def test_a_tool_that_lied_teaches_no_habit(self, core: JarvisCore):
        """Blueprint 7.3's "falsches 'fertig'" must not become a routine."""
        from jarvis.capability.models import ExecutionContext

        core.permissions.issue_grant("m-lie", frozenset({"*"}))
        await core.gateway.execute(
            "demo.unreliable_writer",
            {"path": "/tmp/never.txt"},
            ExecutionContext(correlation_id="c-lie", mission_id="m-lie"),
        )
        habits = await core.memory.entries(type=MemoryType.HABIT)
        assert not any("unreliable_writer" in h.predicate for h in habits)

    async def test_sequences_are_noticed(self, core: JarvisCore):
        await core.handle_command("Systemstatus")
        await core.handle_command("Licht im Office an")
        habits = await core.memory.entries(type=MemoryType.HABIT)
        assert any(h.predicate == "follows:system.status" for h in habits)


class TestPrivacyInTheRunningCore:
    async def test_privacy_mode_stops_conversation_memory(self, core: JarvisCore):
        await core.memory_control.set_privacy(conversation_memory=False)
        await core.handle_command("Licht im Office an")

        episodes = await core.memory.entries(type=MemoryType.EPISODIC)
        assert not any(e.predicate == "text" for e in episodes)

    async def test_privacy_mode_stops_behaviour_learning(self, core: JarvisCore):
        await core.memory_control.set_privacy(learn_behaviour=False)
        await core.handle_command("Licht im Office an")

        assert await core.memory.entries(type=MemoryType.HABIT) == []

    async def test_switching_learning_off_still_records_what_happened(self, core: JarvisCore):
        """Turning off behaviour learning is not turning off the audit trail."""
        await core.memory_control.set_privacy(learn_behaviour=False)
        result = await core.handle_command("Licht im Office an")

        assert result.mission_state == "COMPLETED"
        assert await core.store.list_events(limit=200)

    async def test_a_command_carrying_a_key_is_not_remembered(self, core: JarvisCore):
        fake_key = "sk-ant-" + "not-a-real-key-000"
        await core.handle_command(f"merke dir {fake_key}")

        assert fake_key not in json.dumps([e.to_dict() for e in await core.memory.entries()])


class TestContextInTheAgentPath:
    async def test_context_is_handed_to_the_provider(self, config: CoreConfig):
        from dataclasses import dataclass, field

        from jarvis.agents.provider import AgentRequest, AgentResponse

        @dataclass
        class Recorder:
            name: str = "recorder"
            seen: list[AgentRequest] = field(default_factory=list)

            async def plan(self, request, *, model, effort):
                self.seen.append(request)
                return AgentResponse(summary="noted", done=True)

        recorder = Recorder()
        core = JarvisCore(config, provider=recorder)
        await core.start()
        try:
            await core.handle_command("Plane bitte meinen Umzug nach Lissabon")
            assert recorder.seen
            context = recorder.seen[0].context
            assert "memories" in context
            assert "withheld" in context
            assert context["destination"] in ("local", "cloud")
        finally:
            await core.stop()

    async def test_the_rules_provider_routes_context_locally(self, core: JarvisCore):
        # config.provider defaults to "rules", which is the local provider.
        assert core.config.offline is True

    async def test_a_secret_memory_is_withheld_from_a_cloud_context(self, core: JarvisCore):
        from jarvis.context.builder import Destination
        from jarvis.memory.models import Observation, Source
        from jarvis.memory.privacy import PrivacyFilter

        core.memory.privacy = PrivacyFilter(PrivacySettings(screen_camera_learning=True))
        await core.memory.remember(
            Observation(
                type=MemoryType.VISUAL,
                subject="screen",
                predicate="showed",
                value="account balance",
                source=Source.EXPLICIT_STATEMENT,
                from_screen_or_camera=True,
            )
        )
        built = await core.context.build("account balance", destination=Destination.CLOUD)
        assert built.memories == []
        assert built.withheld_secret == 1


class TestMemorySurvivesRestart:
    async def test_memories_are_readable_by_a_fresh_process(self, config: CoreConfig):
        first = JarvisCore(config)
        await first.start()
        await first.handle_command("Licht im Office an")
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            entries = await second.memory.entries()
            assert any(e.predicate == "repeats:home.set_light" for e in entries)
        finally:
            await second.stop()

    async def test_privacy_settings_survive(self, config: CoreConfig):
        first = JarvisCore(config)
        await first.start()
        await first.memory_control.set_privacy(learn_behaviour=False)
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            # A system that forgot the owner switched learning off would
            # quietly start learning again.
            assert second.memory.privacy.settings.learn_behaviour is False
            await second.handle_command("Licht im Office an")
            assert await second.memory.entries(type=MemoryType.HABIT) == []
        finally:
            await second.stop()

    async def test_routine_proposals_survive(self, config: CoreConfig):
        from jarvis.memory.learning import params_signature
        from jarvis.memory.models import Source, new_entry

        first = JarvisCore(config)
        await first.start()
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:home.set_light",
            value=params_signature({"room": "office", "state": "on"}),
            source=Source.EXPLICIT_STATEMENT,
        )
        for _ in range(3):
            entry = entry.with_observation(source=Source.EXPLICIT_STATEMENT, value=entry.value)
        await first.memory._maybe_propose(entry)
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            assert await second.memory_control.pending_routines()
        finally:
            await second.stop()

    async def test_forgetting_survives(self, config: CoreConfig):
        first = JarvisCore(config)
        await first.start()
        await first.handle_command("Licht im Office an")
        await first.memory_control.forget_window(60)
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            assert await second.memory.entries() == []
        finally:
            await second.stop()


class TestMemoryApi:
    """Blueprint 8.4's control surface, over the local API."""

    def test_the_list_is_served(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            body = client.get("/memory").json()
            assert body["summary"]["total"] > 0
            assert body["entries"]

    def test_search(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            hits = client.get("/memory/search", params={"q": "licht"}).json()
            assert hits
            assert "score" in hits[0]

    def test_correct_and_pin(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            entry = client.get("/memory").json()["entries"][0]

            corrected = client.post(
                f"/memory/{entry['memory_id']}/correct", json={"value": "changed"}
            ).json()
            assert corrected["value"] == "changed"
            assert corrected["source"] == "correction"

            assert client.post(f"/memory/{entry['memory_id']}/pin").json()["pinned"] is True

    def test_forget_one(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            entry = client.get("/memory").json()["entries"][0]
            receipt = client.post(f"/memory/{entry['memory_id']}/forget").json()
            assert receipt["deleted_count"] == 1

    def test_forget_window(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            receipt = client.post("/memory/forget-window", json={"minutes": 30}).json()
            assert receipt["deleted_count"] > 0
            assert receipt["window_minutes"] == 30
            assert client.get("/memory").json()["summary"]["total"] == 0

    def test_dont_learn_this(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            client.post("/memory/dont-learn", json={"subject": "owner", "predicate": "*"})
            client.post("/command", json={"text": "Licht im Office an"})

            entries = client.get("/memory").json()["entries"]
            assert not any(e["subject"] == "owner" for e in entries)

    def test_privacy_switches(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            assert client.get("/memory/privacy").json()["screen_camera_learning"] is False

            updated = client.post("/memory/privacy", json={"conversation_memory": False}).json()
            assert updated["conversation_memory"] is False
            assert updated["learn_behaviour"] is True

    def test_missing_memory_returns_404(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            assert client.post("/memory/nope/pin").status_code == 404

    def test_routines_endpoint_starts_empty(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            assert client.get("/memory/routines").json() == []

    def test_deleting_via_the_api_is_audited_without_the_content(self, config: CoreConfig):
        with TestClient(create_app(config=config)) as client:
            client.post("/command", json={"text": "Licht im Office an"})
            client.post("/memory/forget-window", json={"minutes": 30})

            audit = client.get("/audit", params={"limit": 200}).json()
            assert audit["chain_valid"] is True
            assert any(e["action"] == "memory.delete" for e in audit["entries"])
            # The forgotten command text must not survive in the audit log.
            assert "Licht im Office an" not in json.dumps(
                [e for e in audit["entries"] if e["action"] == "memory.delete"]
            )
