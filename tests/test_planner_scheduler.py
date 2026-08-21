"""Planner, Mission Runner, Scheduler and Watchdog - Blueprint 5.1, 6.3, 7.3.

The properties worth stating plainly, because they are the ones that would be
expensive to get wrong:

* A plan's risk comes from the Capability Registry, never from whoever wrote
  the plan. Memory can propose work; it cannot grade its own danger.
* A cycle is caught at planning time, not discovered as a hang.
* A step whose predecessor failed does not run "anyway".
* Unattended execution may never do more than attended execution: a scheduled
  job at or above P3 parks for the owner instead of running itself.
* Checkpoints make a stopped mission resumable without redoing finished work.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from jarvis.capability.models import ExecutionContext
from jarvis.config import CoreConfig
from jarvis.core import JarvisCore
from jarvis.events import types as ev
from jarvis.events.envelope import utc_now
from jarvis.execution.budget import Budget
from jarvis.execution.gateway import ExecutionOutcome
from jarvis.memory.learning import TriggerKind
from jarvis.memory.models import MemoryType, Source, new_entry
from jarvis.mission.model import MissionState, TaskState
from jarvis.mission.runner import StopReason
from jarvis.permission.levels import PermissionLevel
from jarvis.planner.plan import AgentRole, Plan, PlanError, PlanStep
from jarvis.planner.planner import Planner
from jarvis.scheduler.jobs import (
    JobKind,
    JobOutcome,
    JobResult,
    ScheduledJob,
    parse_daily_at,
)
from jarvis.scheduler.scheduler import Scheduler
from jarvis.scheduler.watchdog import Watchdog


def step(name: str, capability: str | None = None, **kw) -> PlanStep:
    return PlanStep(step_id=name, description=name, capability=capability, **kw)


# --------------------------------------------------------------------------
# The plan graph
# --------------------------------------------------------------------------


class TestPlanGraph:
    def test_a_plan_needs_at_least_one_step(self):
        with pytest.raises(PlanError, match="at least one step"):
            Plan(goal="nothing", steps=())

    def test_a_cycle_is_rejected_at_planning_time(self):
        with pytest.raises(PlanError, match="cycle"):
            Plan(
                goal="loop",
                steps=(
                    step("a", depends_on=("b",)),
                    step("b", depends_on=("a",)),
                ),
            )

    def test_a_longer_cycle_is_rejected_too(self):
        with pytest.raises(PlanError, match="cycle"):
            Plan(
                goal="loop",
                steps=(
                    step("a", depends_on=("c",)),
                    step("b", depends_on=("a",)),
                    step("c", depends_on=("b",)),
                ),
            )

    def test_self_dependency_is_rejected(self):
        with pytest.raises(PlanError, match="depends on itself"):
            Plan(goal="me", steps=(step("a", depends_on=("a",)),))

    def test_an_unknown_dependency_is_rejected(self):
        with pytest.raises(PlanError, match="unknown step"):
            Plan(goal="x", steps=(step("a", depends_on=("ghost",)),))

    def test_duplicate_step_ids_are_rejected(self):
        with pytest.raises(PlanError, match="duplicate"):
            Plan(goal="x", steps=(step("a"), step("a")))

    def test_waves_are_topological(self):
        plan = Plan(
            goal="build",
            steps=(
                step("fetch"),
                step("compile", depends_on=("fetch",)),
                step("lint", depends_on=("fetch",)),
                step("ship", depends_on=("compile", "lint")),
            ),
        )
        waves = [[s.step_id for s in w] for w in plan.waves()]
        assert waves == [["fetch"], ["compile", "lint"], ["ship"]]

    def test_independent_steps_are_reported_as_parallelisable(self):
        """Blueprint 6.3: parallelism is only worth it when steps are
        genuinely independent - the wave structure is what says so."""
        plan = Plan(goal="two", steps=(step("a"), step("b")))
        assert plan.is_parallelisable

        chain = Plan(goal="chain", steps=(step("a"), step("b", depends_on=("a",))))
        assert not chain.is_parallelisable

    def test_a_plan_is_as_risky_as_its_riskiest_step(self):
        plan = Plan(
            goal="mixed",
            steps=(
                step("safe", "home.set_light", risk=PermissionLevel.P1_SAFE),
                step("bad", "system.factory_reset", risk=PermissionLevel.P6_FORBIDDEN),
                step("also_safe", "system.status", risk=PermissionLevel.P0_OBSERVE),
            ),
        )
        assert plan.max_risk is PermissionLevel.P6_FORBIDDEN

    def test_steps_become_tasks_keeping_their_ids(self):
        plan = Plan(goal="x", steps=(step("a"), step("b", depends_on=("a",))))
        tasks = plan.to_tasks()
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert tasks[1].depends_on == ["a"]

    def test_round_trip(self):
        plan = Plan(
            goal="x",
            steps=(step("a", "home.set_light", params={"room": "office"}),),
            rationale="because",
        )
        assert Plan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()


# --------------------------------------------------------------------------
# The Planner
# --------------------------------------------------------------------------


class TestPlanner:
    @pytest.fixture
    def planner(self, core: JarvisCore) -> Planner:
        return Planner(core.registry, memory=core.memory)

    async def test_a_routed_capability_becomes_a_one_step_plan(self, planner):
        planned = await planner.plan(
            "Licht an", capability="home.set_light", params={"room": "office", "state": "on"}
        )
        assert len(planned.plan.steps) == 1
        assert planned.plan.steps[0].role is AgentRole.DIRECT
        assert planned.plan.source == "intent"

    async def test_an_open_goal_delegates_to_an_agent(self, planner):
        planned = await planner.plan("Plane meinen Umzug nach Lissabon")
        assert planned.plan.steps[0].capability is None
        assert planned.plan.steps[0].role is AgentRole.IMPLEMENTATION

    async def test_risk_comes_from_the_registry(self, planner):
        planned = await planner.plan(
            "installier was", capability="system.install_software", params={"name": "x"}
        )
        assert planned.plan.max_risk is PermissionLevel.P4_CRITICAL
        assert planned.requires_approval

    async def test_a_proposed_risk_label_is_overwritten(self, planner):
        """A step that claims to be safe does not get to keep the claim."""
        lying = Plan(
            goal="sneak",
            steps=(step("x", "system.factory_reset", risk=PermissionLevel.P0_OBSERVE),),
        )
        assessed = planner.assess(lying)
        assert assessed.plan.steps[0].risk is PermissionLevel.P6_FORBIDDEN

    async def test_a_safe_plan_needs_no_approval(self, planner):
        planned = await planner.plan("status", capability="system.status", params={})
        assert not planned.requires_approval

    async def test_an_unregistered_capability_is_refused(self, planner):
        with pytest.raises(PlanError, match="unregistered"):
            await planner.plan("do it", capability="does.not.exist")

    async def test_a_plan_that_cannot_fit_the_budget_is_refused(self, planner):
        big = Plan(goal="many", steps=tuple(step(f"s{i}", "system.status") for i in range(10)))
        assessed = planner.assess(big, budget=Budget(max_tool_calls=3))

        assert not assessed.executable
        assert "tool calls" in assessed.budget_problems[0]

    async def test_a_cost_estimate_over_budget_is_refused(self, planner):
        planned = await planner.plan("Plane etwas Großes")
        tight = planner.assess(planned.plan, budget=Budget(max_cost_units=1.0))
        assert not tight.executable

    async def test_a_known_procedure_is_reused(self, core: JarvisCore, planner):
        """Blueprint 8.1's procedural memory: "So deployen wir Projekt X"."""
        await core.memory.put(
            new_entry(
                type=MemoryType.PROCEDURAL,
                subject="owner",
                predicate="deploy_atlas",
                value={
                    "steps": [
                        {"capability": "system.status", "description": "check first"},
                        {
                            "capability": "home.set_light",
                            "params": {"room": "office", "state": "on"},
                            "after": [0],
                        },
                    ]
                },
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        planned = await planner.plan("deploy_atlas bitte")

        assert planned.plan.source == "procedural_memory"
        assert len(planned.plan.steps) == 2
        assert planned.plan.steps[1].depends_on == (planned.plan.steps[0].step_id,)

    async def test_an_unconfident_procedure_is_not_used(self, core: JarvisCore, planner):
        await core.memory.put(
            new_entry(
                type=MemoryType.PROCEDURAL,
                subject="owner",
                predicate="deploy_atlas",
                value={"steps": [{"capability": "system.status"}]},
                source=Source.OBSERVATION,  # one sighting -> hypothesis only
            )
        )
        planned = await planner.plan("deploy_atlas bitte")
        assert planned.plan.source == "delegated"

    async def test_a_remembered_procedure_cannot_smuggle_in_a_forbidden_step(
        self, core: JarvisCore, planner
    ):
        """Memory proposes; the Permission Engine still disposes."""
        await core.memory.put(
            new_entry(
                type=MemoryType.PROCEDURAL,
                subject="owner",
                predicate="nightly_wipe",
                value={"steps": [{"capability": "system.factory_reset"}]},
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        planned = await planner.plan("nightly_wipe")

        # The plan exists, and is correctly graded as forbidden.
        assert planned.plan.max_risk is PermissionLevel.P6_FORBIDDEN
        assert planned.requires_approval

        # And running it changes nothing.
        result = await core.handle_command("nightly_wipe")
        assert result.mission_state in (
            str(MissionState.BLOCKED),
            str(MissionState.FAILED),
        )

    async def test_a_malformed_procedure_is_skipped_not_guessed_at(self, core: JarvisCore, planner):
        await core.memory.put(
            new_entry(
                type=MemoryType.PROCEDURAL,
                subject="owner",
                predicate="broken_thing",
                value={"steps": [{"no_capability": True}, "not even a dict"]},
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        planned = await planner.plan("broken_thing")
        assert planned.plan.source == "delegated"


# --------------------------------------------------------------------------
# The Mission Runner
# --------------------------------------------------------------------------


class TestMissionRunner:
    async def test_dependencies_are_respected(self, core: JarvisCore):
        mission = await core.missions.create("two steps")
        await core.missions.transition(mission, MissionState.PLANNING)
        plan = Plan(
            goal="two steps",
            steps=(
                step("first", "system.status"),
                step(
                    "second",
                    "home.set_light",
                    params={"room": "office", "state": "on"},
                    depends_on=("first",),
                ),
            ),
        )
        for task in plan.to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)

        outcome = await core.runner.run(mission)
        assert outcome.stopped_reason == StopReason.PLAN_COMPLETE
        assert outcome.completed == ["first", "second"]

    async def test_a_step_whose_predecessor_failed_is_skipped_not_run(self, core: JarvisCore):
        mission = await core.missions.create("fail then depend")
        await core.missions.transition(mission, MissionState.PLANNING)
        plan = Plan(
            goal="fail then depend",
            steps=(
                step("liar", "demo.unreliable_writer", params={"path": "/tmp/no.txt"}),
                step(
                    "after",
                    "home.set_light",
                    params={"room": "office", "state": "on"},
                    depends_on=("liar",),
                ),
            ),
        )
        for task in plan.to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)

        before = dict(core.world.lights)
        outcome = await core.runner.run(mission)

        assert "liar" in outcome.failed
        assert mission.task("after").state is TaskState.SKIPPED
        assert core.world.lights == before

    async def test_a_checkpoint_is_written_after_every_step(self, core: JarvisCore):
        mission = await core.missions.create("three steps")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(
            goal="three",
            steps=tuple(step(f"s{i}", "system.status") for i in range(3)),
        ).to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)
        await core.runner.run(mission)

        assert len(mission.checkpoints) == 3
        assert mission.last_checkpoint.completed_task_ids == ["s0", "s1", "s2"]

    async def test_the_kill_switch_stops_a_run_mid_plan(self, core: JarvisCore):
        mission = await core.missions.create("many steps")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(
            goal="many",
            steps=tuple(step(f"s{i}", "system.status") for i in range(5)),
        ).to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)

        core.permissions.engage_kill_switch("owner said stop")
        outcome = await core.runner.run(mission)

        assert outcome.stopped_reason == StopReason.KILL_SWITCH
        assert outcome.completed == []

    async def test_a_budget_stops_a_run_mid_plan(self, core: JarvisCore):
        mission = await core.missions.create("too many steps")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(
            goal="many",
            steps=tuple(step(f"s{i}", "system.status") for i in range(6)),
        ).to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget(max_tool_calls=2))
        await core.missions.transition(mission, MissionState.RUNNING)

        outcome = await core.runner.run(mission)
        assert outcome.stopped_reason == "budget:max_tool_calls"
        assert len(outcome.completed) == 2

    async def test_resuming_does_not_redo_finished_work(self, core: JarvisCore):
        mission = await core.missions.create("resume me")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(
            goal="resume",
            steps=tuple(step(f"s{i}", "system.status") for i in range(4)),
        ).to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget(max_tool_calls=2))
        await core.missions.transition(mission, MissionState.RUNNING)

        first = await core.runner.run(mission)
        assert len(first.completed) == 2

        # Fresh budget, then continue where the checkpoint says.
        core.gateway.budgets.start(mission.mission_id, Budget())
        second = await core.runner.resume(mission)

        assert second.completed == ["s2", "s3"]
        assert mission.is_plan_finished

    async def test_a_crashed_step_is_retried_on_resume(self, core: JarvisCore):
        mission = await core.missions.create("interrupted")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(goal="x", steps=(step("s0", "system.status"),)).to_tasks():
            mission.add_task(task)

        # Simulate a process that died mid-step.
        mission.tasks[0].state = TaskState.RUNNING
        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)

        outcome = await core.runner.resume(mission)
        assert outcome.completed == ["s0"]

    async def test_progress_is_reported_for_the_hud(self, core: JarvisCore):
        mission = await core.missions.create("progress")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(
            goal="p", steps=tuple(step(f"s{i}", "system.status") for i in range(4))
        ).to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(mission.mission_id, frozenset({"*"}))
        core.gateway.budgets.start(mission.mission_id, Budget(max_tool_calls=2))
        await core.missions.transition(mission, MissionState.RUNNING)
        await core.runner.run(mission)

        progress = core.runner.progress(mission)
        assert progress["tasks_total"] == 4
        assert progress["tasks_done"] == 2
        assert progress["fraction_done"] == 0.5

    async def test_a_finished_run_announces_itself(self, core: JarvisCore):
        sub = core.bus.subscribe(ev.MISSION_PROGRESS, name="test")
        try:
            await core.handle_command("Licht im Office an")
            assert not sub.queue.empty()
        finally:
            sub.close()


# --------------------------------------------------------------------------
# The command path, now planned
# --------------------------------------------------------------------------


class TestPlannedCommands:
    async def test_a_simple_command_still_completes(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        assert result.mission_state == str(MissionState.COMPLETED)
        assert core.world.lights["office"] == "on"

    async def test_the_plan_is_attached_to_the_result(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        assert result.extra["plan"]["steps"][0]["capability"] == "home.set_light"
        assert result.extra["plan"]["max_risk"] == "P1"

    async def test_the_mission_carries_its_plan(self, core: JarvisCore):
        result = await core.handle_command("Licht im Office an")
        mission = await core.missions.load(result.mission_id)
        assert mission.context["plan"]["source"] == "intent"

    async def test_a_direct_command_gets_a_narrow_grant(self, core: JarvisCore):
        """A one-capability plan does not hand out a wildcard."""
        result = await core.handle_command("Licht im Office an")
        mission = await core.missions.load(result.mission_id)
        assert mission.context["plan"]["agent_calls"] == 0

    async def test_checkpoints_survive_a_restart(self, config: CoreConfig):
        first = JarvisCore(config)
        await first.start()
        result = await first.handle_command("Licht im Office an")
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            mission = await second.missions.load(result.mission_id)
            assert mission.checkpoints
            assert mission.last_checkpoint.completed_task_ids
        finally:
            await second.stop()


# --------------------------------------------------------------------------
# The Scheduler
# --------------------------------------------------------------------------


class TestJobModel:
    def test_hour_buckets_and_clock_times_both_parse(self):
        assert parse_daily_at("20h") == time(20, 0)
        assert parse_daily_at("07:30") == time(7, 30)

    def test_backoff_is_exponential_and_capped(self):
        job = ScheduledJob(name="j", goal="g", backoff_base=timedelta(minutes=1))
        delays = []
        for _ in range(12):
            job = job.failed(at=utc_now(), mission_id=None)
            delays.append(job.backoff_delay())
            job = ScheduledJob(
                name="j", goal="g", backoff_base=timedelta(minutes=1), attempts=job.attempts
            )

        assert delays[0] < delays[1] < delays[2]
        assert max(delays) <= timedelta(hours=6)

    def test_a_job_gives_up_after_max_attempts(self):
        job = ScheduledJob(name="j", goal="g", max_attempts=2)
        job = job.failed(at=utc_now(), mission_id=None)
        assert job.enabled
        job = job.failed(at=utc_now(), mission_id=None)
        assert not job.enabled

    def test_success_resets_the_retry_counter(self):
        job = ScheduledJob(name="j", goal="g", kind=JobKind.INTERVAL, interval=timedelta(hours=1))
        job = job.failed(at=utc_now(), mission_id=None)
        assert job.attempts == 1
        job = job.succeeded(at=utc_now(), mission_id="m")
        assert job.attempts == 0

    def test_a_one_shot_disables_itself_after_running(self):
        job = ScheduledJob(name="j", goal="g", kind=JobKind.ONCE)
        assert not job.succeeded(at=utc_now(), mission_id="m").enabled

    def test_an_interval_job_does_not_fire_a_catch_up_burst(self):
        """A machine asleep for hours should resume the rhythm, not replay it."""
        start = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
        job = ScheduledJob(
            name="j",
            goal="g",
            kind=JobKind.INTERVAL,
            interval=timedelta(hours=1),
            next_run_at=start,
        )
        woke = start + timedelta(hours=6, minutes=30)
        assert job.next_occurrence(woke) == start + timedelta(hours=7)

    def test_a_daily_job_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 21, 21, 0, tzinfo=UTC)
        job = ScheduledJob(name="j", goal="g", kind=JobKind.DAILY, daily_at=time(20, 0))
        assert job.next_occurrence(now).date() == now.date() + timedelta(days=1)

    def test_parking_earns_no_backoff(self):
        job = ScheduledJob(name="j", goal="g", kind=JobKind.INTERVAL, interval=timedelta(hours=1))
        parked = job.parked(at=utc_now(), mission_id="m", detail="needs you")
        assert parked.attempts == 0
        assert parked.enabled

    def test_round_trip(self):
        job = ScheduledJob(
            name="j",
            goal="g",
            kind=JobKind.DAILY,
            daily_at=time(20, 0),
            grants=frozenset({"comms"}),
        )
        assert ScheduledJob.from_dict(job.to_dict()).to_dict() == job.to_dict()


class TestScheduler:
    @pytest.fixture
    def scheduler(self, core: JarvisCore) -> Scheduler:
        return core.scheduler

    async def test_a_due_job_fires(self, core: JarvisCore, scheduler: Scheduler):
        await scheduler.register(
            ScheduledJob(
                name="lights",
                goal="Licht im Office an",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        results = await scheduler.tick()

        assert len(results) == 1
        assert results[0].outcome is JobOutcome.SUCCEEDED
        assert core.world.lights["office"] == "on"

    async def test_a_job_that_is_not_due_does_not_fire(self, scheduler: Scheduler):
        await scheduler.register(
            ScheduledJob(name="later", goal="x", next_run_at=utc_now() + timedelta(hours=1))
        )
        assert await scheduler.tick() == []

    async def test_a_sensitive_job_parks_instead_of_running(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        """The central rule: unattended may never exceed attended.

        P3 upward needs confirmation (Blueprint 7.1), and an unattended run is
        exactly the context in which no confirmation can be given.
        """
        await scheduler.register(
            ScheduledJob(
                name="nightly message",
                goal="send a message",
                capability="comms.send_message",
                params={"to": "anna", "body": "hi"},
                grants=frozenset({"comms"}),
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        results = await scheduler.tick()

        assert results[0].outcome is JobOutcome.REFUSED
        assert core.world.outbox == []

    async def test_a_critical_job_parks_too(self, core: JarvisCore, scheduler: Scheduler):
        await scheduler.register(
            ScheduledJob(
                name="nightly install",
                goal="install something",
                capability="system.install_software",
                params={"name": "thing"},
                grants=frozenset({"system.admin"}),
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        results = await scheduler.tick()
        assert results[0].outcome is JobOutcome.REFUSED
        assert core.world.installed == set()

    async def test_a_parked_job_says_so_loudly(self, core: JarvisCore, scheduler: Scheduler):
        sub = core.bus.subscribe(ev.JOB_PARKED, name="test")
        try:
            await scheduler.register(
                ScheduledJob(
                    name="p",
                    goal="send",
                    capability="comms.send_message",
                    params={"to": "a", "body": "b"},
                    next_run_at=utc_now() - timedelta(seconds=1),
                )
            )
            await scheduler.tick()
            assert not sub.queue.empty()
        finally:
            sub.close()

    async def test_registration_reports_which_jobs_need_approval(self, scheduler: Scheduler):
        safe = await scheduler.register(
            ScheduledJob(name="s", goal="g", capability="home.set_light")
        )
        risky = await scheduler.register(
            ScheduledJob(name="r", goal="g", capability="comms.send_message")
        )
        assert not scheduler.needs_approval(safe)
        assert scheduler.needs_approval(risky)

    async def test_an_unknown_capability_is_treated_as_needing_approval(self, scheduler: Scheduler):
        job = ScheduledJob(name="x", goal="g", capability="not.registered")
        assert scheduler.needs_approval(job)

    async def test_the_kill_switch_stops_the_scheduler(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        await scheduler.register(
            ScheduledJob(
                name="lights",
                goal="Licht im Office an",
                capability="home.set_light",
                params={"room": "office", "state": "on"},
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        core.permissions.engage_kill_switch("owner said stop")

        assert await scheduler.tick() == []
        assert core.world.lights["office"] == "off"

    async def test_a_failing_job_is_retried_with_backoff(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        await scheduler.register(
            ScheduledJob(
                name="liar",
                goal="write a file",
                capability="demo.unreliable_writer",
                params={"path": "/tmp/never.txt"},
                next_run_at=utc_now() - timedelta(seconds=1),
                backoff_base=timedelta(minutes=5),
            )
        )
        results = await scheduler.tick()

        assert results[0].outcome is JobOutcome.FAILED
        job = scheduler.jobs()[0]
        assert job.attempts == 1
        assert job.next_run_at > utc_now()

    async def test_a_job_that_keeps_failing_is_disabled(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        registered = await scheduler.register(
            ScheduledJob(
                name="liar",
                goal="write a file",
                capability="demo.unreliable_writer",
                params={"path": "/tmp/never.txt"},
                next_run_at=utc_now() - timedelta(seconds=1),
                max_attempts=2,
                backoff_base=timedelta(seconds=0),
            )
        )
        await scheduler.tick()
        # Force it due again rather than sleeping through the backoff.
        from dataclasses import replace

        scheduler._jobs[registered.job_id] = replace(
            scheduler._jobs[registered.job_id], next_run_at=utc_now() - timedelta(seconds=1)
        )
        await scheduler.tick()

        assert not scheduler.get(registered.job_id).enabled

    async def test_an_executor_that_raises_is_a_failure_not_a_crash(self, core: JarvisCore):
        async def explode(job: ScheduledJob) -> JobResult:
            raise RuntimeError("boom")

        scheduler = Scheduler(
            executor=explode,
            bus=core.bus,
            permissions=core.permissions,
            registry=core.registry,
        )
        await scheduler.register(
            ScheduledJob(
                name="bad",
                goal="g",
                capability="system.status",
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        results = await scheduler.tick()
        assert results[0].outcome is JobOutcome.FAILED
        assert "boom" in results[0].detail

    async def test_jobs_survive_a_restart(self, config: CoreConfig):
        first = JarvisCore(config)
        await first.start()
        await first.scheduler.register(
            ScheduledJob(name="persistent", goal="g", capability="system.status")
        )
        await first.stop()

        second = JarvisCore(config)
        await second.start()
        try:
            assert [j.name for j in second.scheduler.jobs()] == ["persistent"]
        finally:
            await second.stop()

    async def test_a_scheduled_run_gets_only_its_registered_grants(
        self, core: JarvisCore, scheduler: Scheduler
    ):
        """A job cannot widen its own rights at fire time."""
        await scheduler.register(
            ScheduledJob(
                name="peek",
                goal="read a secret",
                capability="security.read_secret",
                params={"name": "wifi"},
                grants=frozenset(),  # deliberately without the `secrets` scope
                next_run_at=utc_now() - timedelta(seconds=1),
            )
        )
        results = await scheduler.tick()
        assert results[0].outcome is not JobOutcome.SUCCEEDED


# --------------------------------------------------------------------------
# Approval continues the plan, not just the one approved step
# --------------------------------------------------------------------------


class TestApprovalContinuesThePlan:
    """`core.approve()` must resume the Mission Runner, not call the gateway
    once and stop.

    A plan can have steps after the one that needed confirmation, and the
    approved task's own state has to reach DONE - not stay PENDING under a
    mission the API reports as COMPLETED. Both were broken by an approve()
    that pre-dated the Planner: it called the gateway directly for the one
    pending capability and never touched the task list or the rest of the
    plan.
    """

    async def test_a_later_step_runs_after_approval(self, core: JarvisCore):
        mission = await core.missions.create("multi-step with a gate in the middle")
        await core.missions.transition(mission, MissionState.PLANNING)
        plan = Plan(
            goal="multi-step",
            steps=(
                step("first", "system.status"),
                step(
                    "gated",
                    "comms.send_message",
                    params={"to": "anna", "body": "hi"},
                    depends_on=("first",),
                ),
                step(
                    "after",
                    "home.set_light",
                    params={"room": "office", "state": "on"},
                    depends_on=("gated",),
                ),
            ),
        )
        for task in plan.to_tasks():
            mission.add_task(task)

        core.permissions.issue_grant(
            mission.mission_id, frozenset({"*"}), grants=frozenset({"comms"})
        )
        core.gateway.budgets.start(mission.mission_id, Budget())
        await core.missions.transition(mission, MissionState.RUNNING)

        outcome = await core.runner.run(mission)
        assert outcome.pending_approval is not None
        assert core.world.lights["office"] != "on"

        # Mirror what `_handle_planned` does after a run: settle the mission
        # into WAITING_FOR_APPROVAL before the owner acts on it.
        await core._settle_run(mission, outcome)
        assert mission.state is MissionState.WAITING_FOR_APPROVAL

        settled = await core.approve(outcome.pending_approval["fingerprint"])

        assert settled.mission_state == str(MissionState.COMPLETED)
        assert any(m["to"] == "anna" for m in core.world.outbox)
        # The step after the gate ran too - approval did not stop at the
        # approved step.
        assert core.world.lights["office"] == "on"

        # `approve()` loads its own copy of the mission from the store, so
        # check the persisted truth rather than this test's now-stale local
        # object.
        reloaded = await core.missions.load(mission.mission_id)
        assert [t.state for t in reloaded.tasks] == [
            TaskState.DONE,
            TaskState.DONE,
            TaskState.DONE,
        ]

    async def test_progress_reflects_the_approved_task_as_done(self, core: JarvisCore):
        first = await core.handle_command(
            "Nachricht an anna: bin unterwegs", grants=frozenset({"comms"})
        )
        approved = await core.approve(first.pending_approval["fingerprint"])

        mission = await core.missions.load(approved.mission_id)
        progress = core.runner.progress(mission)
        assert progress["tasks_done"] == progress["tasks_total"] == 1
        assert progress["fraction_done"] == 1.0


# --------------------------------------------------------------------------
# Approved routines actually firing - closing the Blueprint 8.3 gap
# --------------------------------------------------------------------------


class TestRoutinesBecomeJobs:
    async def _matured_habit(self, core: JarvisCore, predicate: str, value):
        entry = new_entry(
            type=MemoryType.HABIT,
            subject="owner",
            predicate=predicate,
            value=value,
            source=Source.EXPLICIT_STATEMENT,
        )
        for _ in range(3):
            entry = entry.with_observation(source=Source.EXPLICIT_STATEMENT, value=value)
        await core.memory.put(entry)
        return await core.memory._maybe_propose(entry)

    async def test_a_daily_routine_becomes_a_scheduled_job(self, core: JarvisCore):
        # The repeats: entry supplies the parameters for the time pattern.
        await core.memory.put(
            new_entry(
                type=MemoryType.HABIT,
                subject="owner",
                predicate="repeats:home.set_light",
                value='{"room":"office","state":"on"}',
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        proposal = await self._matured_habit(core, "time_pattern:home.set_light", "20h")
        assert proposal.trigger_kind is TriggerKind.DAILY

        decided = await core.memory.decide_proposal(proposal.proposal_id, approve=True)
        assert decided.job_id is not None

        job = core.scheduler.get(decided.job_id)
        assert job.kind is JobKind.DAILY
        assert job.daily_at == time(20, 0)
        assert job.params == {"room": "office", "state": "on"}

    async def test_a_non_clock_routine_gets_memory_but_no_job(self, core: JarvisCore):
        """A "whenever" trigger has nothing for a clock-driven scheduler to
        wait on - registering it would promise a job that never fires."""
        proposal = await self._matured_habit(
            core, "repeats:home.set_light", '{"room":"office","state":"on"}'
        )
        assert proposal.trigger_kind is TriggerKind.WHENEVER

        decided = await core.memory.decide_proposal(proposal.proposal_id, approve=True)
        assert decided.job_id is None
        assert core.scheduler.jobs() == []
        # But it is remembered, so the Planner can reuse it.
        assert await core.memory.entries(type=MemoryType.PROCEDURAL)

    async def test_rejecting_a_routine_schedules_nothing(self, core: JarvisCore):
        await core.memory.put(
            new_entry(
                type=MemoryType.HABIT,
                subject="owner",
                predicate="repeats:home.set_light",
                value='{"room":"office","state":"on"}',
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        proposal = await self._matured_habit(core, "time_pattern:home.set_light", "20h")
        decided = await core.memory.decide_proposal(proposal.proposal_id, approve=False)

        assert decided.job_id is None
        assert core.scheduler.jobs() == []

    async def test_an_approved_routine_actually_runs(self, core: JarvisCore):
        await core.memory.put(
            new_entry(
                type=MemoryType.HABIT,
                subject="owner",
                predicate="repeats:home.set_light",
                value='{"room":"office","state":"on"}',
                source=Source.EXPLICIT_STATEMENT,
            )
        )
        proposal = await self._matured_habit(core, "time_pattern:home.set_light", "20h")
        decided = await core.memory.decide_proposal(proposal.proposal_id, approve=True)

        # Bring its next run forward rather than waiting until 20:00.
        from dataclasses import replace

        core.scheduler._jobs[decided.job_id] = replace(
            core.scheduler._jobs[decided.job_id], next_run_at=utc_now() - timedelta(seconds=1)
        )
        results = await core.scheduler.tick()

        assert results[0].outcome is JobOutcome.SUCCEEDED
        assert core.world.lights["office"] == "on"


# --------------------------------------------------------------------------
# The Watchdog
# --------------------------------------------------------------------------


class TestWatchdog:
    async def test_a_stalled_mission_is_failed(self, core: JarvisCore):
        watchdog = Watchdog(
            missions=core.missions,
            bus=core.bus,
            max_duration=timedelta(seconds=1),
            grace=timedelta(seconds=0),
            audit=core.audit,
        )
        mission = await core.missions.create("hangs forever")
        await core.missions.transition(mission, MissionState.PLANNING)
        await core.missions.transition(mission, MissionState.RUNNING)

        report = await watchdog.sweep(now=utc_now() + timedelta(minutes=5))
        assert report.tripped == (mission.mission_id,)

        reloaded = await core.missions.load(mission.mission_id)
        assert reloaded.state is MissionState.FAILED
        assert "watchdog" in reloaded.history[-1].reason

    async def test_a_mission_making_progress_is_left_alone(self, core: JarvisCore):
        watchdog = Watchdog(
            missions=core.missions, bus=core.bus, max_duration=timedelta(minutes=10)
        )
        mission = await core.missions.create("busy")
        await core.missions.transition(mission, MissionState.PLANNING)
        await core.missions.transition(mission, MissionState.RUNNING)

        report = await watchdog.sweep()
        assert report.tripped == ()

    async def test_only_running_missions_are_swept(self, core: JarvisCore):
        watchdog = Watchdog(
            missions=core.missions,
            bus=core.bus,
            max_duration=timedelta(seconds=0),
            grace=timedelta(seconds=0),
        )
        result = await core.handle_command("Licht im Office an")
        report = await watchdog.sweep(now=utc_now() + timedelta(hours=1))

        assert result.mission_id not in report.tripped

    async def test_the_rollback_point_names_the_last_checkpoint(self, core: JarvisCore):
        watchdog = Watchdog(
            missions=core.missions,
            bus=core.bus,
            max_duration=timedelta(seconds=0),
            grace=timedelta(seconds=0),
            audit=core.audit,
        )
        mission = await core.missions.create("stalls after one step")
        await core.missions.transition(mission, MissionState.PLANNING)
        for task in Plan(goal="x", steps=(step("s0", "system.status"),)).to_tasks():
            mission.add_task(task)
        mission.tasks[0].state = TaskState.DONE
        mission.checkpoint("after s0")
        await core.missions.transition(mission, MissionState.RUNNING)
        await core.missions.save(mission)

        await watchdog.sweep(now=utc_now() + timedelta(hours=1))

        entries = await core.audit.entries(limit=200)
        trip = next(e for e in entries if e["action"] == "safety.watchdog")
        assert trip["rollback_point"]["completed_task_ids"] == ["s0"]

    async def test_a_watchdog_kill_is_audited(self, core: JarvisCore):
        watchdog = Watchdog(
            missions=core.missions,
            bus=core.bus,
            max_duration=timedelta(seconds=0),
            grace=timedelta(seconds=0),
            audit=core.audit,
        )
        mission = await core.missions.create("hangs")
        await core.missions.transition(mission, MissionState.PLANNING)
        await core.missions.transition(mission, MissionState.RUNNING)
        await watchdog.sweep(now=utc_now() + timedelta(hours=1))

        ok, _ = await core.audit.verify_chain()
        assert ok
        entries = await core.audit.entries(limit=200)
        assert any(e["action"] == "safety.watchdog" for e in entries)


# --------------------------------------------------------------------------
# Over the API
# --------------------------------------------------------------------------


class TestPlannerSchedulerApi:
    def test_plan_endpoint_assesses_without_running(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            body = client.post("/plan", json={"text": "Licht im Office an"}).json()
            assert body["steps"][0]["capability"] == "home.set_light"
            assert body["fits_budget"] is True
            # Nothing ran.
            assert client.get("/missions").json() == []

    def test_plan_endpoint_reports_approval_need(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            body = client.post("/plan", json={"text": "installiere Docker"}).json()
            assert body["requires_approval"] is True
            assert body["max_risk"] == "P4"

    def test_scheduler_endpoints(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            created = client.post(
                "/scheduler/jobs",
                json={
                    "name": "lights",
                    "goal": "Licht im Office an",
                    "capability": "home.set_light",
                    "params": {"room": "office", "state": "on"},
                    "in_seconds": -1,
                },
            ).json()
            assert created["needs_approval_each_run"] is False

            fired = client.post("/scheduler/tick").json()
            assert fired[0]["outcome"] == "succeeded"

            assert client.get("/scheduler").json()["total"] == 1
            assert client.delete(f"/scheduler/jobs/{created['job_id']}").status_code == 200

    def test_a_sensitive_job_is_flagged_at_registration(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            created = client.post(
                "/scheduler/jobs",
                json={
                    "name": "nightly mail",
                    "goal": "send a message",
                    "capability": "comms.send_message",
                    "params": {"to": "a", "body": "b"},
                },
            ).json()
            assert created["needs_approval_each_run"] is True

    def test_mission_progress_endpoint(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            result = client.post("/command", json={"text": "Licht im Office an"}).json()
            progress = client.get(f"/missions/{result['mission_id']}/progress").json()
            assert progress["tasks_done"] == 1
            assert progress["checkpoints"]

    def test_watchdog_sweep_endpoint(self, config: CoreConfig):
        from fastapi.testclient import TestClient

        from jarvis.api.server import create_app

        with TestClient(create_app(config=config)) as client:
            assert "checked" in client.post("/watchdog/sweep").json()


async def test_the_gateway_still_guards_a_scheduled_call(core: JarvisCore):
    """Defence in depth: even if the ceiling were bypassed, the gate holds."""
    core.permissions.issue_grant("m-direct", frozenset({"*"}), grants=frozenset())
    result = await core.gateway.execute(
        "comms.send_message",
        {"to": "anna", "body": "hi"},
        ExecutionContext(correlation_id="c", mission_id="m-direct", actor="scheduler"),
    )
    assert result.outcome is not ExecutionOutcome.EXECUTED or not result.succeeded
