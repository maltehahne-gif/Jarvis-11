"""Event envelope, bus, audit chain, mission state machine and persistence."""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.audit.logger import GENESIS, AuditEntry, AuditLogger, compute_hash
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, Sensitivity
from jarvis.mission.engine import MissionEngine
from jarvis.mission.model import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    Mission,
    MissionState,
    Task,
)
from jarvis.persistence.sqlite_store import SqliteStore


class TestEventEnvelope:
    """Blueprint 5.2 - the field set is a contract, not an implementation detail."""

    def test_it_carries_every_documented_field(self):
        payload = Event(type="mission.task.started").to_dict()
        assert set(payload) == {
            "event_id",
            "type",
            "timestamp",
            "source",
            "correlation_id",
            "user_id",
            "device_id",
            "sensitivity",
            "priority",
            "payload",
            "ttl",
        }

    def test_defaults_are_conservative(self):
        event = Event(type="x")
        assert event.sensitivity is Sensitivity.PRIVATE
        assert event.priority is Priority.NORMAL
        assert event.user_id == "local-owner"

    def test_round_trip(self):
        original = Event(
            type="tool.invoked",
            payload={"a": 1},
            sensitivity=Sensitivity.SECRET,
            priority=Priority.CRITICAL,
            ttl=60,
        )
        assert Event.from_dict(json.loads(original.to_json())) == original

    def test_it_is_immutable(self):
        import dataclasses

        event = Event(type="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.type = "y"  # type: ignore[misc]

    def test_derive_keeps_the_correlation_chain(self):
        parent = Event(type="command.received", device_id="phone-1")
        child = parent.derive("command.routed")
        assert child.correlation_id == parent.correlation_id
        assert child.device_id == "phone-1"
        assert child.event_id != parent.event_id


class TestEventBus:
    async def test_pattern_subscription(self):
        bus = EventBus()
        sub = bus.subscribe("mission.*", name="test")
        await bus.publish(Event(type="mission.created"))
        await bus.publish(Event(type="tool.invoked"))

        assert sub.queue.get_nowait().type == "mission.created"
        assert sub.queue.empty()
        sub.close()

    async def test_persist_happens_before_fan_out(self):
        """A subscriber must never see an event that was not stored first."""
        order: list[str] = []

        async def sink(event: Event) -> None:
            order.append("sink")

        bus = EventBus(sink=sink)

        @bus.on("*")
        async def handler(event: Event) -> None:
            order.append("handler")

        await bus.publish(Event(type="x"))
        assert order == ["sink", "handler"]

    async def test_a_failing_handler_does_not_break_the_producer(self):
        bus = EventBus()

        @bus.on("*")
        async def bad(event: Event) -> None:
            raise RuntimeError("boom")

        sub = bus.subscribe("*")
        await bus.publish(Event(type="x"))
        assert sub.queue.get_nowait().type == "x"

    async def test_a_full_subscriber_drops_instead_of_blocking(self):
        """Fluid-first: a slow consumer must not stall the producer."""
        bus = EventBus(queue_size=1)
        sub = bus.subscribe("*", name="slow")

        await asyncio.wait_for(
            asyncio.gather(*(bus.publish(Event(type=f"e{i}")) for i in range(5))),
            timeout=1.0,
        )
        assert sub.dropped >= 1
        assert bus.published_count == 5

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        sub = bus.subscribe("*")
        sub.close()
        await bus.publish(Event(type="x"))
        assert sub.queue.empty()


class TestAuditChain:
    """Blueprint 7.2 - tamper-evident history."""

    async def test_entries_chain_from_genesis(self, store: SqliteStore):
        audit = AuditLogger(store)
        first = await audit.log(
            action="a", actor="core", subject="s", decision="allow", correlation_id="c"
        )
        second = await audit.log(
            action="b", actor="core", subject="s", decision="allow", correlation_id="c"
        )
        assert first["prev_hash"] == GENESIS
        assert second["prev_hash"] == first["entry_hash"]

    async def test_a_clean_chain_verifies(self, store: SqliteStore):
        audit = AuditLogger(store)
        for i in range(5):
            await audit.log(
                action=f"a{i}", actor="core", subject="s", decision="allow", correlation_id="c"
            )
        ok, broken = await audit.verify_chain()
        assert ok and broken is None

    async def test_editing_a_past_entry_is_detected(self, store: SqliteStore):
        audit = AuditLogger(store)
        for i in range(3):
            await audit.log(
                action=f"a{i}", actor="core", subject="s", decision="allow", correlation_id="c"
            )

        # Tamper directly in storage, the way an attacker with disk access would.
        rows = await store._read("SELECT seq, document FROM audit ORDER BY seq")
        doc = json.loads(rows[1]["document"])
        doc["decision"] = "allow-but-actually-not"
        await store._write(
            "UPDATE audit SET document = ? WHERE seq = ?",
            (json.dumps(doc, sort_keys=True), rows[1]["seq"]),
        )

        ok, broken = await audit.verify_chain()
        assert not ok
        assert broken == doc["entry_id"]

    async def test_deleting_an_entry_is_detected(self, store: SqliteStore):
        audit = AuditLogger(store)
        for i in range(3):
            await audit.log(
                action=f"a{i}", actor="core", subject="s", decision="allow", correlation_id="c"
            )
        rows = await store._read("SELECT seq FROM audit ORDER BY seq")
        await store._write("DELETE FROM audit WHERE seq = ?", (rows[1]["seq"],))

        ok, _ = await audit.verify_chain()
        assert not ok

    async def test_concurrent_writers_do_not_fork_the_chain(self, store: SqliteStore):
        """Two writers at once must not both chain onto the same entry.

        A triggered routine writes its audit entry while the command that set
        it off is still writing its own. Unserialised, both read the same tip
        and the log forks - two entries claiming one predecessor - which
        `verify_chain` reports exactly as it reports tampering. A busy system
        would then raise a permanent false alarm, and the one signal that has
        to mean something would stop meaning it (Blueprint 7.2).
        """
        audit = AuditLogger(store)
        await asyncio.gather(
            *(
                audit.log(
                    action=f"a{i}", actor="core", subject="s", decision="allow", correlation_id="c"
                )
                for i in range(25)
            )
        )

        ok, broken = await audit.verify_chain()
        assert ok, f"chain forked at {broken}"

        records = await store.list_audit(limit=100)
        assert len(records) == 25
        # Every entry but the first is some other entry's successor, exactly once.
        parents = [r["prev_hash"] for r in records]
        assert len(set(parents)) == len(parents)

    def test_hashing_is_canonical_regardless_of_key_order(self):
        a = {"x": 1, "y": 2}
        b = {"y": 2, "x": 1}
        assert compute_hash(a, GENESIS) == compute_hash(b, GENESIS)

    async def test_critical_state_carries_prev_state_and_rollback(self, store: SqliteStore):
        audit = AuditLogger(store)
        record = await audit.record(
            AuditEntry(
                action="tool.executed",
                actor="core",
                subject="files.move",
                decision="allow",
                correlation_id="c",
                prev_state={"path": "/a"},
                rollback_point={"undo": "files.move"},
            )
        )
        assert record["prev_state"] == {"path": "/a"}
        assert record["rollback_point"] == {"undo": "files.move"}


class TestMissionStateMachine:
    """Blueprint 5.3."""

    def test_the_happy_path_from_the_blueprint(self):
        mission = Mission(goal="test")
        for state in (
            MissionState.PLANNING,
            MissionState.WAITING_FOR_APPROVAL,
            MissionState.RUNNING,
            MissionState.VERIFYING,
            MissionState.COMPLETED,
        ):
            mission.transition(state)
        assert mission.state is MissionState.COMPLETED

    def test_illegal_jumps_are_refused(self):
        mission = Mission(goal="test")
        with pytest.raises(IllegalTransition):
            mission.transition(MissionState.COMPLETED)

    def test_terminal_states_are_final(self):
        for terminal in TERMINAL_STATES:
            assert LEGAL_TRANSITIONS[terminal] == frozenset()

    def test_a_completed_mission_cannot_be_restarted(self):
        mission = Mission(goal="test")
        mission.transition(MissionState.PLANNING)
        mission.transition(MissionState.RUNNING)
        mission.transition(MissionState.VERIFYING)
        mission.transition(MissionState.COMPLETED)
        with pytest.raises(IllegalTransition):
            mission.transition(MissionState.RUNNING)

    def test_verification_may_send_a_mission_back_to_running(self):
        mission = Mission(goal="test")
        mission.transition(MissionState.PLANNING)
        mission.transition(MissionState.RUNNING)
        mission.transition(MissionState.VERIFYING)
        mission.transition(MissionState.RUNNING, "goal not reached, retrying")
        assert mission.state is MissionState.RUNNING

    def test_every_transition_is_recorded(self):
        mission = Mission(goal="test")
        mission.transition(MissionState.PLANNING, "routed")
        mission.transition(MissionState.CANCELED, "owner changed their mind")
        assert [h.to_dict()["to"] for h in mission.history] == [
            "CREATED",
            "PLANNING",
            "CANCELED",
        ]
        assert mission.history[-1].reason == "owner changed their mind"

    def test_round_trip_preserves_everything(self):
        mission = Mission(goal="test", device_id="desk-01")
        mission.transition(MissionState.PLANNING)
        mission.add_task(Task(description="step", capability="system.status"))

        restored = Mission.from_dict(json.loads(json.dumps(mission.to_dict())))
        assert restored.state is mission.state
        assert restored.goal == mission.goal
        assert restored.device_id == mission.device_id
        assert len(restored.tasks) == 1
        assert len(restored.history) == len(mission.history)


class TestPersistence:
    async def test_events_survive_and_filter(self, store: SqliteStore):
        await store.append_event(Event(type="a.b", correlation_id="c1"))
        await store.append_event(Event(type="a.c", correlation_id="c2"))

        assert len(await store.list_events(correlation_id="c1")) == 1
        assert len(await store.list_events(type_prefix="a.")) == 2

    async def test_appending_the_same_event_twice_is_idempotent(self, store: SqliteStore):
        event = Event(type="x")
        await store.append_event(event)
        await store.append_event(event)
        assert len(await store.list_events()) == 1

    async def test_missions_upsert(self, store: SqliteStore):
        await store.save_mission("m1", "CREATED", {"mission_id": "m1", "updated_at": "1"})
        await store.save_mission("m1", "RUNNING", {"mission_id": "m1", "updated_at": "2"})

        assert len(await store.list_missions()) == 1
        assert len(await store.list_missions(state="RUNNING")) == 1
        assert len(await store.list_missions(state="CREATED")) == 0

    async def test_state_slots_round_trip(self, store: SqliteStore):
        await store.put_state("k", {"a": 1})
        assert await store.get_state("k") == {"a": 1}
        assert await store.get_state("missing") is None

    async def test_using_a_closed_store_fails_loudly(self, tmp_path):
        s = SqliteStore(tmp_path / "x.db")
        with pytest.raises(RuntimeError):
            await s.append_event(Event(type="x"))

    async def test_mission_engine_persists_before_publishing(self, store: SqliteStore):
        """A crash between the two may lose the notification, never the fact."""
        seen: list[str] = []
        bus = EventBus(sink=store.append_event)

        @bus.on("mission.state.changed")
        async def check(event: Event) -> None:
            record = await store.load_mission(event.payload["mission_id"])
            seen.append(record["state"])

        engine = MissionEngine(store=store, bus=bus)
        mission = await engine.create("goal")
        await engine.transition(mission, MissionState.PLANNING)

        # The store already had the new state when the event was handled.
        assert seen == ["PLANNING"]
