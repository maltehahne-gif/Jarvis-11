"""Memory and personalisation - Blueprint 8, plus the Context Builder from 5.1.

Organised by pipeline stage, following figure 3: observations enter, the
Privacy Filter decides, the stores hold, the graph and index retrieve, and the
Context Builder hands a *subset* onward.

The security-shaped tests are the ones that matter most here, and they are
stated as properties rather than mechanics: credential material is never
stored, `SECRET` knowledge never reaches a cloud-bound context, a deletion
never leaves its content behind in the audit log, and a noticed pattern never
becomes a behaviour without the owner saying so.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from jarvis.context.builder import ContextBuilder, Destination
from jarvis.events.envelope import Sensitivity, utc_now
from jarvis.memory.control import MemoryControl
from jarvis.memory.index import KnowledgeGraph, LexicalIndex, tokenize
from jarvis.memory.learning import (
    MIN_OBSERVATIONS_FOR_PROPOSAL,
    ProposalStatus,
    observations_for_action,
    params_signature,
    propose_from,
    time_bucket,
)
from jarvis.memory.models import (
    PERSONALISATION_THRESHOLD,
    MemoryEntry,
    MemoryType,
    Observation,
    Retention,
    Source,
    new_entry,
)
from jarvis.memory.privacy import FilterRule, PrivacyFilter, PrivacySettings, raise_sensitivity
from jarvis.memory.service import MemoryService
from jarvis.security.redaction import contains_credential, looks_like_credential

FAKE_KEY = "sk-ant-" + "not-a-real-key-000"


@pytest.fixture
async def memory(store) -> MemoryService:
    service = MemoryService(store=store, state_store=store)
    await service.load()
    return service


def observation(**kw) -> Observation:
    defaults = dict(
        type=MemoryType.PREFERENCE,
        subject="owner",
        predicate="preferred_editor",
        value="VS Code",
        source=Source.OBSERVATION,
    )
    defaults.update(kw)
    return Observation(**defaults)


# --------------------------------------------------------------------------
# Blueprint 8.2 - the entry itself
# --------------------------------------------------------------------------


class TestMemoryEntry:
    def test_carries_every_documented_field(self):
        payload = new_entry(
            type=MemoryType.PREFERENCE,
            subject="owner",
            predicate="preferred_editor",
            value="VS Code",
        ).to_dict()
        for field in (
            "memory_id",
            "type",
            "subject",
            "predicate",
            "value",
            "confidence",
            "source",
            "observations",
            "created_at",
            "last_confirmed_at",
            "sensitivity",
            "retention",
            "project_scope",
        ):
            assert field in payload, field

    def test_all_eight_types_from_8_1_exist(self):
        assert {str(t) for t in MemoryType} >= {
            "working",
            "episodic",
            "semantic",
            "project",
            "preference",
            "habit",
            "relationship",
            "procedural",
        }

    def test_round_trip(self):
        entry = new_entry(
            type=MemoryType.SEMANTIC,
            subject="Projekt Atlas",
            predicate="uses",
            value="PostgreSQL",
            source=Source.EXPLICIT_STATEMENT,
        )
        assert MemoryEntry.from_dict(json.loads(json.dumps(entry.to_dict()))) == entry

    def test_it_is_immutable(self):
        import dataclasses

        entry = new_entry(type=MemoryType.PREFERENCE, subject="owner", predicate="p", value="v")
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.confidence = 1.0  # type: ignore[misc]


class TestConfidence:
    """Blueprint 8.3: "Beobachtung -> Muster -> Hypothese -> Confidence"."""

    def test_a_single_observation_is_only_a_hypothesis(self):
        entry = new_entry(type=MemoryType.PREFERENCE, subject="owner", predicate="p", value="v")
        assert entry.confidence < PERSONALISATION_THRESHOLD
        assert not entry.actionable

    def test_repetition_earns_confidence(self):
        entry = new_entry(type=MemoryType.PREFERENCE, subject="owner", predicate="p", value="v")
        for _ in range(6):
            entry = entry.with_observation(source=Source.OBSERVATION, value="v")
        assert entry.actionable
        assert entry.observations == 7

    def test_an_explicit_statement_is_believed_immediately(self):
        entry = new_entry(
            type=MemoryType.PREFERENCE,
            subject="owner",
            predicate="p",
            value="v",
            source=Source.EXPLICIT_STATEMENT,
        )
        assert entry.actionable

    def test_a_correction_overrides_the_value_outright(self):
        entry = new_entry(
            type=MemoryType.PREFERENCE,
            subject="owner",
            predicate="preferred_editor",
            value="Vim",
            source=Source.EXPLICIT_STATEMENT,
        )
        corrected = entry.corrected_to("VS Code")
        assert corrected.value == "VS Code"
        assert corrected.source is Source.CORRECTION
        assert corrected.actionable

    def test_one_stray_observation_does_not_overwrite_a_settled_belief(self):
        entry = new_entry(
            type=MemoryType.PREFERENCE,
            subject="owner",
            predicate="p",
            value="settled",
            source=Source.EXPLICIT_STATEMENT,
        )
        contradicted = entry.with_observation(source=Source.OBSERVATION, value="other")
        assert contradicted.value == "settled"
        assert contradicted.confidence < entry.confidence

    def test_a_belief_that_keeps_being_contradicted_eventually_gives_way(self):
        entry = new_entry(type=MemoryType.PREFERENCE, subject="owner", predicate="p", value="old")
        for _ in range(5):
            entry = entry.with_observation(source=Source.OBSERVATION, value="new")
        assert entry.value == "new"

    def test_confidence_never_reaches_certainty(self):
        entry = new_entry(
            type=MemoryType.PREFERENCE,
            subject="owner",
            predicate="p",
            value="v",
            source=Source.CORRECTION,
        )
        for _ in range(50):
            entry = entry.with_observation(source=Source.CORRECTION, value="v")
        assert entry.confidence <= 0.99


# --------------------------------------------------------------------------
# Blueprint 8, figure 3 - the Privacy + Sensitivity Filter
# --------------------------------------------------------------------------


class TestPrivacyFilter:
    def test_credential_material_is_never_stored(self):
        verdict = PrivacyFilter().check(observation(value=FAKE_KEY))
        assert not verdict.accepted
        assert verdict.rule is FilterRule.CREDENTIAL_MATERIAL

    def test_credential_material_in_a_predicate_is_caught_too(self):
        verdict = PrivacyFilter().check(observation(predicate=f"key_{FAKE_KEY}"))
        assert not verdict.accepted

    def test_credentials_are_refused_even_from_an_explicit_statement(self):
        verdict = PrivacyFilter().check(
            observation(value=FAKE_KEY, source=Source.EXPLICIT_STATEMENT)
        )
        assert not verdict.accepted

    def test_behaviour_learning_can_be_switched_off(self):
        settings = PrivacySettings(learn_behaviour=False)
        verdict = PrivacyFilter(settings).check(observation(type=MemoryType.PREFERENCE))
        assert verdict.rule is FilterRule.PRIVACY_MODE_BEHAVIOUR

    def test_switching_off_behaviour_still_allows_plain_facts(self):
        settings = PrivacySettings(learn_behaviour=False)
        verdict = PrivacyFilter(settings).check(
            observation(type=MemoryType.SEMANTIC, subject="Atlas", predicate="uses")
        )
        assert verdict.accepted

    def test_conversation_memory_can_be_switched_off(self):
        settings = PrivacySettings(conversation_memory=False)
        verdict = PrivacyFilter(settings).check(observation(from_conversation=True))
        assert verdict.rule is FilterRule.PRIVACY_MODE_CONVERSATION

    def test_screen_and_camera_learning_is_off_by_default(self):
        verdict = PrivacyFilter().check(observation(from_screen_or_camera=True))
        assert not verdict.accepted
        assert verdict.rule is FilterRule.PRIVACY_MODE_SCREEN_CAMERA

    def test_dont_learn_this_blocks_a_specific_pair(self):
        settings = PrivacySettings()
        settings.block("owner", "preferred_editor")
        verdict = PrivacyFilter(settings).check(observation())
        assert verdict.rule is FilterRule.DONT_LEARN_THIS

    def test_dont_learn_this_can_block_a_whole_subject(self):
        settings = PrivacySettings()
        settings.block("owner")
        assert not PrivacyFilter(settings).check(observation(predicate="anything")).accepted

    def test_visual_observations_are_classified_secret(self):
        settings = PrivacySettings(screen_camera_learning=True)
        verdict = PrivacyFilter(settings).check(
            observation(type=MemoryType.VISUAL, from_screen_or_camera=True)
        )
        assert verdict.accepted
        assert verdict.sensitivity is Sensitivity.SECRET

    def test_facts_about_other_people_are_classified_secret(self):
        verdict = PrivacyFilter().check(
            observation(type=MemoryType.RELATIONSHIP, subject="Anna", predicate="works_at")
        )
        assert verdict.sensitivity is Sensitivity.SECRET

    def test_sensitivity_can_only_be_raised(self):
        assert raise_sensitivity(Sensitivity.PRIVATE, Sensitivity.SECRET) is Sensitivity.SECRET
        assert raise_sensitivity(Sensitivity.SECRET, Sensitivity.PUBLIC) is Sensitivity.SECRET

    def test_an_empty_observation_is_not_worth_remembering(self):
        assert PrivacyFilter().check(observation(value="")).rule is FilterRule.EMPTY_VALUE


class TestCredentialDetection:
    """Shared by the permission gate and the memory filter - they must agree."""

    @pytest.mark.parametrize(
        "value",
        [FAKE_KEY, "-----BEGIN RSA PRIVATE KEY-----", "AKIAIOSFODNN7EXAMPLE", "ghp_abc123"],
    )
    def test_known_key_shapes_are_detected(self, value):
        assert looks_like_credential(value)

    @pytest.mark.parametrize("value", ["VS Code", "office", "hello world", ""])
    def test_ordinary_text_is_not_flagged(self, value):
        assert not looks_like_credential(value)

    def test_nested_structures_are_walked(self):
        assert contains_credential({"config": {"token": FAKE_KEY}})
        assert contains_credential(["safe", ["deeper", FAKE_KEY]])
        assert not contains_credential({"room": "office", "state": "on"})

    def test_a_cyclic_structure_does_not_hang(self):
        cyclic: dict = {}
        cyclic["self"] = cyclic
        assert contains_credential(cyclic) is False


# --------------------------------------------------------------------------
# Storage and retrieval
# --------------------------------------------------------------------------


class TestRemembering:
    async def test_a_new_observation_creates_an_entry(self, memory: MemoryService):
        result = await memory.remember(observation())
        assert result.stored and result.created
        assert result.entry.value == "VS Code"

    async def test_a_repeat_folds_into_the_same_belief(self, memory: MemoryService):
        first = await memory.remember(observation())
        second = await memory.remember(observation())
        assert not second.created
        assert second.entry.memory_id == first.entry.memory_id
        assert second.entry.observations == 2
        assert second.entry.confidence > first.entry.confidence

    async def test_two_beliefs_about_different_things_stay_separate(self, memory: MemoryService):
        await memory.remember(observation(predicate="preferred_editor", value="VS Code"))
        await memory.remember(observation(predicate="preferred_shell", value="fish"))
        assert len(await memory.entries()) == 2

    async def test_a_rejected_observation_stores_nothing(self, memory: MemoryService):
        result = await memory.remember(observation(value=FAKE_KEY))
        assert not result.stored
        assert await memory.entries() == []

    async def test_project_scope_separates_beliefs(self, memory: MemoryService):
        await memory.remember(
            observation(predicate="db", value="PostgreSQL", project_scope="atlas")
        )
        await memory.remember(observation(predicate="db", value="SQLite", project_scope="jarvis"))
        entries = await memory.entries()
        assert {e.value for e in entries} == {"PostgreSQL", "SQLite"}


class TestRetrieval:
    async def test_finds_a_relevant_memory(self, memory: MemoryService):
        await memory.remember(
            observation(
                type=MemoryType.SEMANTIC,
                subject="Projekt Atlas",
                predicate="verwendet",
                value="PostgreSQL",
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        hits = await memory.recall("Welche Datenbank nutzt Projekt Atlas?")
        assert hits
        assert hits[0].entry.value == "PostgreSQL"
        assert "atlas" in hits[0].matched_terms

    async def test_an_irrelevant_query_returns_nothing(self, memory: MemoryService):
        await memory.remember(observation())
        assert await memory.recall("Wetter in Lissabon") == []

    async def test_confidence_influences_ranking(self, store):
        service = MemoryService(store=store, state_store=store)
        await service.remember(
            observation(
                subject="topic", predicate="weak", value="postgres", source=Source.OBSERVATION
            )
        )
        await service.remember(
            observation(
                subject="topic",
                predicate="strong",
                value="postgres",
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        hits = await service.recall("postgres")
        assert hits[0].entry.predicate == "strong"

    async def test_expired_entries_are_not_returned(self, memory: MemoryService):
        result = await memory.remember(observation())
        await memory.put(result.entry.made_temporary(timedelta(seconds=-1)))
        assert await memory.recall("VS Code") == []

    def test_tokenizer_drops_filler_words(self):
        assert tokenize("Bitte die Datenbank von Atlas") == {"datenbank", "von", "atlas"}

    async def test_the_graph_answers_what_do_we_know_about(self, store):
        service = MemoryService(store=store, state_store=store)
        await service.remember(
            observation(
                type=MemoryType.PROJECT, subject="Atlas", predicate="uses", value="Postgres"
            )
        )
        await service.remember(
            observation(
                type=MemoryType.PROJECT, subject="Atlas", predicate="deploys_to", value="fly"
            )
        )
        graph = KnowledgeGraph(store)
        assert len(await graph.about("Atlas")) == 2

    async def test_the_graph_walks_edges_from_either_end(self, store):
        service = MemoryService(store=store, state_store=store)
        await service.remember(
            observation(
                type=MemoryType.PROJECT, subject="Atlas", predicate="uses", value="Postgres"
            )
        )
        graph = KnowledgeGraph(store)
        assert len(await graph.neighbours("Postgres")) == 1

    async def test_the_index_is_a_port_the_core_depends_on(self, store):
        from jarvis.memory.index import MemoryIndex

        assert isinstance(LexicalIndex(store), MemoryIndex)


# --------------------------------------------------------------------------
# Blueprint 8.3 - the learning loop
# --------------------------------------------------------------------------


class TestLearningLoop:
    def test_a_habit_needs_repetition_before_it_is_proposed(self):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:home.set_light",
            value=params_signature({"room": "office", "state": "on"}),
            source=Source.EXPLICIT_STATEMENT,
        )
        assert entry.observations < MIN_OBSERVATIONS_FOR_PROPOSAL
        assert propose_from(entry) is None

    def test_a_repeated_confident_habit_becomes_a_proposal(self):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:home.set_light",
            value=params_signature({"room": "office", "state": "on"}),
        )
        for _ in range(6):
            entry = entry.with_observation(source=Source.OBSERVATION, value=entry.value)
        proposal = propose_from(entry)
        assert proposal is not None
        assert proposal.capability == "home.set_light"
        assert proposal.params == {"room": "office", "state": "on"}
        assert proposal.status is ProposalStatus.PROPOSED

    def test_a_shaky_pattern_never_reaches_the_owner(self):
        entry = new_entry(type=MemoryType.HABIT, subject="owner", predicate="repeats:x", value="{}")
        for _ in range(4):
            entry = entry.with_observation(source=Source.OBSERVATION, value="different")
        if entry.confidence < PERSONALISATION_THRESHOLD:
            assert propose_from(entry) is None

    def test_daily_patterns_are_bucketed_by_hour(self):
        assert time_bucket(utc_now().replace(hour=18)) == "18h"

    def test_one_action_generates_repetition_and_time_observations(self):
        observations = observations_for_action("home.set_light", {"room": "office"}, at=utc_now())
        predicates = {o.predicate for o in observations}
        assert any(p.startswith("repeats:") for p in predicates)
        assert any(p.startswith("time_pattern:") for p in predicates)

    def test_a_following_action_generates_a_sequence_observation(self):
        observations = observations_for_action(
            "files.move", {}, at=utc_now(), previous_capability="system.status"
        )
        assert any(o.predicate == "follows:system.status" for o in observations)

    def test_different_parameters_are_different_habits(self):
        assert params_signature({"room": "office"}) != params_signature({"room": "bedroom"})

    async def test_a_proposal_does_nothing_until_approved(self, memory: MemoryService):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:home.set_light",
            value=params_signature({"room": "office", "state": "on"}),
        )
        for _ in range(6):
            entry = entry.with_observation(source=Source.OBSERVATION, value=entry.value)
        proposal = await memory._maybe_propose(entry)

        assert proposal is not None
        assert proposal.status is ProposalStatus.PROPOSED
        # Nothing procedural was written: the routine does not exist yet.
        assert await memory.entries(type=MemoryType.PROCEDURAL) == []

    async def test_approval_records_the_routine_as_procedural_memory(self, memory: MemoryService):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:home.set_light",
            value=params_signature({"room": "office", "state": "on"}),
        )
        for _ in range(6):
            entry = entry.with_observation(source=Source.OBSERVATION, value=entry.value)
        proposal = await memory._maybe_propose(entry)

        decided = await memory.decide_proposal(proposal.proposal_id, approve=True)
        assert decided.status is ProposalStatus.APPROVED
        assert len(await memory.entries(type=MemoryType.PROCEDURAL)) == 1

    async def test_rejection_leaves_nothing_behind(self, memory: MemoryService):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate="repeats:x",
            value="{}",
            source=Source.EXPLICIT_STATEMENT,
        )
        for _ in range(3):
            entry = entry.with_observation(source=Source.EXPLICIT_STATEMENT, value="{}")
        proposal = await memory._maybe_propose(entry)

        decided = await memory.decide_proposal(proposal.proposal_id, approve=False)
        assert decided.status is ProposalStatus.REJECTED
        assert await memory.entries(type=MemoryType.PROCEDURAL) == []


# --------------------------------------------------------------------------
# Blueprint 5.1 - the Context Builder
# --------------------------------------------------------------------------


class TestContextBuilder:
    async def test_secret_knowledge_never_reaches_a_cloud_context(self, memory: MemoryService):
        settings = PrivacySettings(screen_camera_learning=True)
        memory.privacy = PrivacyFilter(settings)
        await memory.remember(
            Observation(
                type=MemoryType.VISUAL,
                subject="screen",
                predicate="showed",
                value="banking dashboard",
                source=Source.EXPLICIT_STATEMENT,
                from_screen_or_camera=True,
            )
        )
        builder = ContextBuilder(memory)

        cloud = await builder.build("what was on the banking screen", destination=Destination.CLOUD)
        assert cloud.memories == []
        assert cloud.withheld_secret == 1

        local = await builder.build("what was on the banking screen", destination=Destination.LOCAL)
        assert len(local.memories) == 1

    async def test_relationship_facts_stay_local_too(self, memory: MemoryService):
        await memory.remember(
            Observation(
                type=MemoryType.RELATIONSHIP,
                subject="Anna",
                predicate="works_at",
                value="Acme",
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        builder = ContextBuilder(memory)
        cloud = await builder.build("where does Anna work", destination=Destination.CLOUD)
        assert cloud.withheld_secret == 1
        assert cloud.memories == []

    async def test_a_hypothesis_does_not_steer_behaviour(self, memory: MemoryService):
        await memory.remember(observation())  # single sighting, low confidence
        built = await ContextBuilder(memory).build("preferred editor")
        assert built.memories == []
        assert built.withheld_low_confidence == 1

    async def test_a_confirmed_belief_is_included(self, memory: MemoryService):
        await memory.remember(observation(source=Source.EXPLICIT_STATEMENT))
        built = await ContextBuilder(memory).build("which editor do I prefer")
        assert len(built.memories) == 1
        assert built.memories[0]["value"] == "VS Code"

    async def test_the_budget_is_respected(self, memory: MemoryService):
        for i in range(20):
            await memory.remember(
                observation(
                    predicate=f"fact_{i}",
                    value=f"postgres detail {i}",
                    source=Source.EXPLICIT_STATEMENT,
                )
            )
        built = await ContextBuilder(memory, budget_chars=300).build("postgres")
        assert built.used_chars <= 300
        assert built.withheld_budget > 0

    async def test_what_was_withheld_is_reported(self, memory: MemoryService):
        await memory.remember(observation())
        built = await ContextBuilder(memory).build("editor")
        assert built.to_dict()["withheld"]["total"] == built.withheld_total

    def test_an_unknown_provider_is_treated_as_cloud(self):
        assert Destination.for_provider("anything-else") is Destination.CLOUD
        assert Destination.for_provider("local") is Destination.LOCAL

    def test_state_is_trimmed_to_what_a_model_can_act_on(self):
        trimmed = ContextBuilder._relevant_state(
            {
                "devices": [{"device_id": "desk-01", "online": True, "trusted": True}],
                "active_missions": ["m1"],
                "presence": "desk-01",
            }
        )
        # Trust flags are the Permission Engine's business, not the model's.
        assert "devices" not in trimmed
        assert "trusted" not in str(trimmed)
        assert trimmed["devices_online"] == 1


# --------------------------------------------------------------------------
# Blueprint 8.4 - "What JARVIS Knows"
# --------------------------------------------------------------------------


class TestControlSurface:
    @pytest.fixture
    async def control(self, memory: MemoryService, store):
        from jarvis.audit.logger import AuditLogger

        audit = AuditLogger(store)
        memory._audit = audit
        return MemoryControl(memory, audit=audit), audit

    async def test_the_list_shows_source_and_confidence(self, memory, control):
        surface, _ = control
        await memory.remember(observation(source=Source.EXPLICIT_STATEMENT))
        rows = await surface.what_jarvis_knows()
        assert rows[0]["source"] == "explicit_statement"
        assert "confidence" in rows[0]
        assert "last_confirmed_at" in rows[0]
        assert "affected_automations" in rows[0]

    async def test_correct(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation(value="Vim"))
        corrected = await surface.correct(result.entry.memory_id, "VS Code")
        assert corrected.value == "VS Code"
        assert corrected.actionable

    async def test_forget_removes_the_entry(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation())
        receipt = await surface.forget(result.entry.memory_id)
        assert receipt.count == 1
        assert await memory.entries() == []

    async def test_pin(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation())
        assert (await surface.pin(result.entry.memory_id)).pinned

    async def test_make_temporary(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation())
        temporary = await surface.make_temporary(result.entry.memory_id, timedelta(hours=2))
        assert temporary.retention is Retention.TEMPORARY
        assert temporary.expires_at is not None

    async def test_dont_learn_this_blocks_and_forgets(self, memory, control):
        surface, _ = control
        await memory.remember(observation())
        receipt = await surface.dont_learn_this("owner", "preferred_editor")

        assert receipt.count == 1
        assert await memory.entries() == []
        # And it stays blocked going forward.
        assert not (await memory.remember(observation())).stored

    async def test_forget_window_clears_recent_memories(self, memory, control):
        surface, _ = control
        await memory.remember(observation(predicate="a", value="1"))
        await memory.remember(observation(predicate="b", value="2"))
        receipt = await surface.forget_window(30)
        assert receipt.count == 2
        assert receipt.window_minutes == 30
        assert await memory.entries() == []

    async def test_forget_window_spares_pinned_entries(self, memory, control):
        surface, _ = control
        keep = await memory.remember(observation(predicate="keep", value="1"))
        await memory.remember(observation(predicate="drop", value="2"))
        await surface.pin(keep.entry.memory_id)

        receipt = await surface.forget_window(30)
        assert receipt.protected_pinned == 1
        remaining = await memory.entries()
        assert len(remaining) == 1
        assert remaining[0].predicate == "keep"

    async def test_forget_window_leaves_older_memories_alone(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation())
        from dataclasses import replace

        old = replace(result.entry, last_confirmed_at=utc_now() - timedelta(hours=3))
        await memory.put(old)

        receipt = await surface.forget_window(30)
        assert receipt.count == 0
        assert len(await memory.entries()) == 1

    async def test_deletion_is_auditable(self, memory, control):
        surface, audit = control
        result = await memory.remember(observation())
        await surface.forget(result.entry.memory_id)

        entries = await audit.entries()
        deletes = [e for e in entries if e["action"] == "memory.delete"]
        assert deletes
        assert deletes[-1]["prev_state"]["deleted_count"] == 1
        ok, _ = await audit.verify_chain()
        assert ok

    async def test_deletion_does_not_leave_the_content_in_the_audit_log(self, memory, control):
        """The audit log is append-only and hash-chained: anything recorded
        there could never be forgotten afterwards."""
        surface, audit = control
        secret_value = "the-thing-to-be-forgotten"
        result = await memory.remember(observation(value=secret_value))
        await surface.forget(result.entry.memory_id)

        assert secret_value not in json.dumps(await audit.entries())

    async def test_forget_window_does_not_leak_content_either(self, memory, control):
        surface, audit = control
        secret_value = "private-note-12345"
        await memory.remember(observation(value=secret_value))
        await surface.forget_window(30)

        assert secret_value not in json.dumps(await audit.entries())

    async def test_privacy_mode_switches_are_independent(self, memory, control):
        surface, _ = control
        await surface.set_privacy(conversation_memory=False)
        settings = memory.privacy.settings
        assert settings.conversation_memory is False
        assert settings.learn_behaviour is True  # untouched

    async def test_privacy_settings_survive_a_reload(self, store):
        service = MemoryService(store=store, state_store=store)
        control = MemoryControl(service)
        await control.set_privacy(learn_behaviour=False)

        revived = MemoryService(store=store, state_store=store)
        await revived.load()
        assert revived.privacy.settings.learn_behaviour is False

    async def test_blocklist_survives_a_reload(self, store):
        service = MemoryService(store=store, state_store=store)
        await MemoryControl(service).dont_learn_this("owner", "preferred_editor")

        revived = MemoryService(store=store, state_store=store)
        await revived.load()
        assert revived.privacy.settings.is_blocked("owner", "preferred_editor")

    async def test_expired_temporary_entries_are_purged(self, memory, control):
        surface, _ = control
        result = await memory.remember(observation())
        await surface.make_temporary(result.entry.memory_id, timedelta(seconds=-1))
        assert len(await memory.purge_expired()) == 1
        assert await memory.entries() == []
