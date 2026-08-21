"""Event-driven routines - Blueprint 8.3's "after" trigger.

The learning loop could always *notice* that one action follows another; what
it could not do was act on it, because the Scheduler only watches a clock.
These tests cover the half that closes that gap, and in particular the three
properties that make it safe to let an action start work on its own:

* an unattended routine may do no more than an attended one (7.1),
* the kill switch stops work that is about to start (7.2), and
* a routine's own action can never set off another routine, so no chain of
  them can close into a loop (7.3's "Agent-Endlosschleife").
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.events import types as ev
from jarvis.events.envelope import Event, utc_now
from jarvis.memory.learning import RoutineProposal, TriggerKind
from jarvis.scheduler.jobs import JobKind, JobOutcome, ScheduledJob
from jarvis.scheduler.scheduler import Scheduler
from jarvis.scheduler.triggers import TriggerWatcher


async def wait_for(predicate, *, timeout: float = 2.0) -> None:
    """Wait for a background task to have had its effect.

    The watcher drains on its own task, so an assertion made immediately
    after publishing would race it. Polling with a deadline keeps the test
    honest about that asynchrony instead of hiding it behind a fixed sleep.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the watcher to act")


def tool_succeeded(
    capability: str,
    *,
    actor: str = "core",
    verification: str = "passed",
    params: dict | None = None,
) -> Event:
    """The event the Execution Gateway publishes after a verified action.

    `core` is the actor a command from the owner actually runs under; the
    scheduler's own runs use `scheduler`, which is the difference the cycle
    guard turns on.
    """
    return Event(
        type=ev.TOOL_SUCCEEDED,
        source="execution-gateway",
        payload={
            "capability": capability,
            "mission_id": "m-1",
            "verification": verification,
            "actor": actor,
            "params": params or {},
        },
    )


class TestAfterJobModel:
    def test_an_after_job_is_never_due_by_the_clock(self):
        """Its whole point is that a time cannot say when it should run."""
        job = ScheduledJob(
            name="j",
            goal="g",
            kind=JobKind.AFTER,
            after_capability="system.status",
            next_run_at=utc_now() - timedelta(days=1),
        )
        assert not job.is_due()

    def test_it_stays_armed_after_a_successful_run(self):
        """Unlike a one-shot: the trigger can happen again tomorrow."""
        job = ScheduledJob(name="j", goal="g", kind=JobKind.AFTER, after_capability="system.status")
        assert job.succeeded(at=utc_now(), mission_id="m").enabled

    def test_it_stays_armed_after_parking(self):
        job = ScheduledJob(name="j", goal="g", kind=JobKind.AFTER, after_capability="system.status")
        parked = job.parked(at=utc_now(), mission_id="m", detail="needs you")
        assert parked.enabled
        assert parked.attempts == 0

    def test_repeated_failure_still_disarms_it(self):
        """The circuit breaker is not about clocks, so it still applies.

        A routine that fails every time it is triggered should stop being
        triggered, exactly as a timed job that fails every night stops.
        """
        job = ScheduledJob(
            name="j",
            goal="g",
            kind=JobKind.AFTER,
            after_capability="system.status",
            max_attempts=2,
        )
        job = job.failed(at=utc_now(), mission_id=None)
        assert job.enabled
        job = job.failed(at=utc_now(), mission_id=None)
        assert not job.enabled

    def test_round_trip(self):
        job = ScheduledJob(name="j", goal="g", kind=JobKind.AFTER, after_capability="system.status")
        assert ScheduledJob.from_dict(job.to_dict()).to_dict() == job.to_dict()


class TestTriggerMatching:
    @pytest.fixture
    def watcher(self, core: JarvisCore) -> TriggerWatcher:
        return core.triggers

    def test_a_verified_owner_action_is_a_trigger(self, watcher: TriggerWatcher):
        assert watcher.should_trigger(tool_succeeded("system.status")) == "system.status"

    def test_an_unverified_action_is_not(self, watcher: TriggerWatcher):
        """ "Tool aufgerufen" is not "Ziel erreicht" (Blueprint 5.4)."""
        event = tool_succeeded("system.status", verification="failed")
        assert watcher.should_trigger(event) is None

    def test_an_unverifiable_action_still_counts(self, watcher: TriggerWatcher):
        """The same standard memory learns from - not everything has a verifier."""
        event = tool_succeeded("system.status", verification="unverifiable")
        assert watcher.should_trigger(event) == "system.status"

    def test_a_routines_own_action_is_not_a_trigger(self, watcher: TriggerWatcher):
        """The cycle guard. Without it two routines could set each other off."""
        assert watcher.should_trigger(tool_succeeded("system.status", actor="scheduler")) is None
        assert watcher.suppressed == 1

    def test_an_event_without_an_actor_is_refused(self, watcher: TriggerWatcher):
        """A missed routine is a disappointment; a routine loop is an incident."""
        event = tool_succeeded("system.status")
        del event.payload["actor"]
        assert watcher.should_trigger(event) is None

    def test_other_event_types_are_ignored(self, watcher: TriggerWatcher):
        assert watcher.should_trigger(Event(type=ev.TOOL_FAILED, source="x")) is None


class TestArmedJobs:
    @pytest.fixture
    def scheduler(self, core: JarvisCore) -> Scheduler:
        return core.scheduler

    async def test_armed_for_finds_only_matching_enabled_jobs(self, scheduler: Scheduler):
        wanted = await scheduler.register(
            ScheduledJob(
                name="wanted",
                goal="g",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
            )
        )
        await scheduler.register(
            ScheduledJob(
                name="other trigger",
                goal="g",
                kind=JobKind.AFTER,
                after_capability="files.move",
                capability="home.set_light",
            )
        )
        await scheduler.register(ScheduledJob(name="timed", goal="g", kind=JobKind.DAILY))

        armed = scheduler.armed_for("system.status")
        assert [j.job_id for j in armed] == [wanted.job_id]

    async def test_a_disabled_job_is_not_armed(self, scheduler: Scheduler):
        job = await scheduler.register(
            ScheduledJob(
                name="off",
                goal="g",
                kind=JobKind.AFTER,
                after_capability="system.status",
                enabled=False,
            )
        )
        assert scheduler.armed_for("system.status") == []
        assert scheduler.get(job.job_id) is not None

    async def test_the_snapshot_does_not_report_an_armed_job_as_a_next_run(
        self, scheduler: Scheduler
    ):
        """Blueprint 7.3: the HUD must not be shown a time nothing happens at."""
        await scheduler.register(
            ScheduledJob(
                name="armed",
                goal="g",
                kind=JobKind.AFTER,
                after_capability="system.status",
                next_run_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        snapshot = scheduler.snapshot()
        assert snapshot["next_run_at"] is None
        assert snapshot["armed"] == 1


class TestFiring:
    @pytest.fixture
    def scheduler(self, core: JarvisCore) -> Scheduler:
        return core.scheduler

    async def test_a_triggered_routine_runs(self, core: JarvisCore, scheduler: Scheduler):
        job = await scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )
        result = await scheduler.fire_now(job.job_id)

        assert result is not None
        assert result.outcome is JobOutcome.SUCCEEDED
        assert core.world.lights["office"] == "on"

    async def test_the_kill_switch_stops_a_trigger(self, core: JarvisCore, scheduler: Scheduler):
        """ "Jarvis, stop everything" covers work about to start (7.2)."""
        job = await scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )
        core.permissions.engage_kill_switch("owner said stop")

        assert await scheduler.fire_now(job.job_id) is None
        assert core.world.lights["office"] == "off"

    async def test_a_sensitive_routine_is_refused_instead_of_run(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        """The ceiling is a property of nobody watching, not of the clock.

        An event-driven routine is still unattended execution, so P3 upward
        gets exactly what a timed job gets: refused up front, because the
        registry already says the level needs a confirmation that no
        unattended run can give.
        """
        job = await scheduler.register(
            ScheduledJob(
                name="message after status",
                goal="send a message",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="comms.send_message",
                params={"to": "anna", "body": "hi"},
                grants=frozenset({"comms"}),
            )
        )
        result = await scheduler.fire_now(job.job_id)

        assert result is not None
        assert result.outcome is JobOutcome.REFUSED
        assert core.world.outbox == []

    async def test_a_refused_routine_stays_armed(self, core: JarvisCore, scheduler: Scheduler):
        """Refusing one firing is not giving up on the routine.

        The owner may approve the parked mission; the next time the trigger
        happens the same question should be put to them again.
        """
        job = await scheduler.register(
            ScheduledJob(
                name="message after status",
                goal="send a message",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="comms.send_message",
                params={"to": "anna", "body": "hi"},
                grants=frozenset({"comms"}),
            )
        )
        await scheduler.fire_now(job.job_id)

        assert [j.name for j in scheduler.armed_for("system.status")] == ["message after status"]

    async def test_an_unknown_job_is_not_an_error(self, scheduler: Scheduler):
        assert await scheduler.fire_now("no-such-job") is None


class TestWatcherEndToEnd:
    async def test_a_matching_event_fires_the_routine(self, core: JarvisCore):
        await core.scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )
        results = await core.triggers.handle(tool_succeeded("system.status"))

        assert [r.outcome for r in results] == [JobOutcome.SUCCEEDED]
        assert core.world.lights["office"] == "on"
        assert core.triggers.fired == 1

    async def test_a_different_capability_fires_nothing(self, core: JarvisCore):
        await core.scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )
        assert await core.triggers.handle(tool_succeeded("files.move")) == []
        assert core.world.lights["office"] == "off"

    async def test_a_routine_cannot_set_off_another_routine(self, core: JarvisCore):
        """Two routines pointed at each other must not loop.

        `a` fires when `system.status` completes and runs `home.set_light`;
        `b` fires when `home.set_light` completes and runs `system.status`.
        Left unguarded that is a closed cycle. The guard is that whatever a
        routine does carries actor "scheduler", which is never a trigger.
        """
        await core.scheduler.register(
            ScheduledJob(
                name="a",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )
        await core.scheduler.register(
            ScheduledJob(
                name="b",
                goal="Status lesen",
                kind=JobKind.AFTER,
                after_capability="home.set_light",
                capability="system.status",
            )
        )

        await core.triggers.handle(tool_succeeded("system.status"))

        # `a` ran once. `b` did not run at all, because the only
        # `home.set_light` completion came from `a`, not from the owner.
        assert core.triggers.fired == 1
        assert core.triggers.suppressed >= 1
        assert core.world.lights["office"] == "on"


class TestThroughTheBus:
    """The wiring the unit tests above bypass by calling `handle` directly."""

    async def test_a_real_command_fires_an_armed_routine(self, core: JarvisCore):
        """The whole path: owner acts, gateway publishes, watcher fires.

        This is the one test that exercises the queue and the drain task, and
        so the one that would catch the watcher never having been subscribed.
        """
        await core.scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )

        await core.handle_command("wie ist der status", device_id="desk-01")
        # Wait on the counter, not the light: the light comes on part-way
        # through the firing, so waiting on it would race the bookkeeping.
        await wait_for(lambda: core.triggers.fired == 1)

        assert core.world.lights["office"] == "on"

    async def test_the_owners_action_is_not_delayed_by_the_routine(self, core: JarvisCore):
        """Principle 4: a routine must not make the owner wait.

        The watcher drains its own queue, so `handle_command` returns while
        the routine is still to run. If the watcher were an inline `bus.on`
        handler, the light would already be on here.
        """
        await core.scheduler.register(
            ScheduledJob(
                name="lights after status",
                goal="Licht im Office an",
                kind=JobKind.AFTER,
                after_capability="system.status",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
            )
        )

        await core.handle_command("wie ist der status", device_id="desk-01")
        assert core.world.lights["office"] == "off"

        # It does still run, just not on the owner's time.
        await wait_for(lambda: core.triggers.fired == 1)
        assert core.world.lights["office"] == "on"


class TestRoutineActivation:
    async def test_approving_an_after_routine_arms_a_job(self, core: JarvisCore):
        proposal = RoutineProposal(
            trigger="after system.status",
            trigger_kind=TriggerKind.AFTER,
            trigger_detail="system.status",
            capability="home.set_light",
            params={"room": "office", "state": "on"},
            rationale="This followed system.status 4 times.",
            observations=4,
            confidence=0.9,
        )
        job_id = await core.activate_routine(proposal)

        assert job_id is not None
        job = core.scheduler.get(job_id)
        assert job is not None
        assert job.kind is JobKind.AFTER
        assert job.after_capability == "system.status"
        assert job.origin == f"routine:{proposal.proposal_id}"

    async def test_an_after_routine_on_an_unknown_capability_is_refused(self, core: JarvisCore):
        """A job that could never fire is worse than no job at all."""
        proposal = RoutineProposal(
            trigger="after nonsense.capability",
            trigger_kind=TriggerKind.AFTER,
            trigger_detail="nonsense.capability",
            capability="home.set_light",
            params={},
            rationale="x",
            observations=4,
            confidence=0.9,
        )
        assert await core.activate_routine(proposal) is None
        assert core.scheduler.jobs() == []

    async def test_a_whenever_routine_still_gets_no_job(self, core: JarvisCore):
        """It names no moment, so nothing can wait for it."""
        proposal = RoutineProposal(
            trigger="whenever you would normally do it",
            trigger_kind=TriggerKind.WHENEVER,
            capability="home.set_light",
            params={},
            rationale="x",
            observations=4,
            confidence=0.9,
        )
        assert not proposal.schedulable
        assert await core.activate_routine(proposal) is None

    async def test_an_armed_routine_survives_a_restart(self, config: CoreConfig):
        """A standing instruction that forgot itself on reboot is not standing."""
        core = JarvisCore(config)
        await core.start()
        try:
            await core.scheduler.register(
                ScheduledJob(
                    name="lights after status",
                    goal="Licht im Office an",
                    kind=JobKind.AFTER,
                    after_capability="system.status",
                    capability="home.set_light",
                    params={"room": "office", "state": "on"},
                )
            )
        finally:
            await core.stop()

        revived = JarvisCore(config)
        await revived.start()
        try:
            armed = revived.scheduler.armed_for("system.status")
            assert [j.name for j in armed] == ["lights after status"]
        finally:
            await revived.stop()
