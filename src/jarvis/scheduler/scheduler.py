"""Scheduler - Blueprint 5.1, with the safety rules from 7.1 and 7.2.

    "Zeitbasierte Jobs, Background Missions, Retry/Backoff."

The mechanics are ordinary: a poll loop, due jobs, exponential backoff. What is
not ordinary is that everything here runs **while nobody is watching**, and
that single fact decides the module's central rule:

    Unattended execution may never do more than attended execution.

Blueprint 7.1 grades actions by how much confirmation they need - P3 wants
"Bestätigung je Kontext", P4 "starke Bestätigung / biometrisch". An unattended
context is precisely one in which no confirmation can be given. So a scheduled
job at or above P3 does not run and does not fail: it *parks*, leaving its
mission in WAITING_FOR_APPROVAL and raising an urgent event, so the owner
decides when they next look. Silently executing it would turn the scheduler
into a way to launder P4 actions past the permission table; silently dropping
it would be a broken promise.

Two more rules follow from the same reasoning:

* **Rights never widen at fire time.** A job carries the grants it was
  registered with. The scheduler passes exactly those to the mission and
  nothing more.
* **The kill switch stops the scheduler.** "Jarvis, stop everything" (7.2)
  must stop work that is about to start, not only work already running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.capability.registry import CapabilityRegistry
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority
from jarvis.permission.engine import PermissionEngine
from jarvis.permission.levels import PermissionLevel
from jarvis.persistence.ports import StateStore
from jarvis.scheduler.jobs import JobOutcome, JobResult, ScheduledJob

log = logging.getLogger(__name__)

JOBS_STATE_KEY = "scheduler.jobs"

#: The highest risk level a job may carry out with nobody present. P2 is the
#: last level Blueprint 7.1 marks "Automatisch" (with undo/log); P3 upward all
#: require a confirmation that an unattended run cannot obtain.
MAX_UNATTENDED_LEVEL = PermissionLevel.P2_REVERSIBLE

#: How often the loop looks for due work. Cheap - a tick with nothing due is a
#: scan of a small dict.
POLL_SECONDS = 1.0

JobExecutor = Callable[[ScheduledJob], Awaitable[JobResult]]


class Scheduler:
    def __init__(
        self,
        *,
        executor: JobExecutor,
        bus: EventBus,
        permissions: PermissionEngine,
        registry: CapabilityRegistry,
        state_store: StateStore | None = None,
        audit: AuditLogger | None = None,
        poll_seconds: float = POLL_SECONDS,
        max_unattended_level: PermissionLevel = MAX_UNATTENDED_LEVEL,
    ) -> None:
        self._executor = executor
        self._bus = bus
        self._permissions = permissions
        self._registry = registry
        self._state_store = state_store
        self._audit = audit
        self._poll_seconds = poll_seconds
        self._max_unattended_level = max_unattended_level
        self._jobs: dict[str, ScheduledJob] = {}
        self._loop: asyncio.Task[None] | None = None
        self._running = False

    # -- registry -----------------------------------------------------------

    async def register(self, job: ScheduledJob) -> ScheduledJob:
        """Store a job. Registration is permissive; *firing* is where the
        risk ceiling applies.

        Refusing to register a nightly deploy would be unhelpful - scheduling
        it is a perfectly reasonable thing to want. What must not happen is
        that it runs itself at 3am without the owner's confirmation, and that
        is enforced in `_fire`.
        """
        self._jobs[job.job_id] = job
        await self._save()

        await self._bus.publish(
            Event(
                type=ev.JOB_REGISTERED,
                source="scheduler",
                payload={
                    **job.to_dict(),
                    "needs_approval_each_run": self.needs_approval(job),
                },
            )
        )
        if self._audit is not None:
            await self._audit.log(
                action="scheduler.register",
                actor="owner",
                subject=job.name,
                decision="registered",
                correlation_id=job.job_id,
                capability=job.capability,
                kind=str(job.kind),
                origin=job.origin,
                needs_approval_each_run=self.needs_approval(job),
            )
        return job

    async def remove(self, job_id: str) -> bool:
        if self._jobs.pop(job_id, None) is None:
            return False
        await self._save()
        return True

    async def set_enabled(self, job_id: str, enabled: bool) -> ScheduledJob | None:
        from dataclasses import replace

        job = self._jobs.get(job_id)
        if job is None:
            return None
        # Re-enabling a job that gave up starts its retry count fresh;
        # otherwise it would fire once and immediately be exhausted again.
        updated = replace(job, enabled=enabled, attempts=0 if enabled else job.attempts)
        self._jobs[job_id] = updated
        await self._save()
        return updated

    def get(self, job_id: str) -> ScheduledJob | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[ScheduledJob]:
        return sorted(self._jobs.values(), key=lambda j: j.next_run_at)

    def due(self, now: datetime | None = None) -> list[ScheduledJob]:
        return [j for j in self.jobs() if j.is_due(now)]

    def needs_approval(self, job: ScheduledJob) -> bool:
        """Whether this job's action exceeds what may run unattended."""
        if job.capability is None:
            # A job that delegates to a reasoning agent cannot be graded up
            # front - whatever it proposes is graded individually by the
            # Permission Engine when the call is actually made.
            return False
        if not self._registry.has(job.capability):
            return True
        return self._registry.get(job.capability).level > self._max_unattended_level

    # -- lifecycle ----------------------------------------------------------

    async def load(self) -> None:
        if self._state_store is None:
            return
        record = await self._state_store.get_state(JOBS_STATE_KEY)
        if record is not None:
            self._jobs = {j["job_id"]: ScheduledJob.from_dict(j) for j in record.get("jobs", [])}

    async def _save(self) -> None:
        if self._state_store is None:
            return
        await self._state_store.put_state(
            JOBS_STATE_KEY, {"jobs": [j.to_dict() for j in self._jobs.values()]}
        )

    async def start(self) -> None:
        if self._loop is not None:
            return
        self._running = True
        self._loop = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop is None:
            return
        self._loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._loop
        self._loop = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_seconds)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A scheduler that dies on one bad job stops every other job
                # too. Log and keep the loop alive.
                log.exception("scheduler tick failed")

    # -- firing -------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> list[JobResult]:
        """Fire everything currently due. Returns one result per job fired."""
        from jarvis.events.envelope import utc_now

        moment = now or utc_now()

        if self._permissions.kill_switch_engaged:
            # Not an error, and not something to retry: the owner said stop.
            return []

        results: list[JobResult] = []
        for job in self.due(moment):
            results.append(await self._fire(job, moment))
        return results

    async def _fire(self, job: ScheduledJob, now: datetime) -> JobResult:
        await self._bus.publish(
            Event(
                type=ev.JOB_FIRED,
                source="scheduler",
                correlation_id=job.job_id,
                device_id=job.device_id,
                payload={"job_id": job.job_id, "name": job.name, "goal": job.goal},
            )
        )

        if self.needs_approval(job):
            return await self._park(
                job,
                now,
                detail=(
                    f"{job.capability} needs owner confirmation; an unattended run cannot give it"
                ),
                refused=True,
            )

        try:
            result = await self._executor(job)
        except Exception as exc:
            log.exception("scheduled job %s raised", job.name)
            result = JobResult(outcome=JobOutcome.FAILED, detail=f"{type(exc).__name__}: {exc}")

        if result.outcome is JobOutcome.PARKED:
            return await self._park(job, now, detail=result.detail, mission_id=result.mission_id)
        if result.outcome is JobOutcome.SUCCEEDED:
            return await self._succeed(job, now, result)
        return await self._fail(job, now, result)

    async def _succeed(self, job: ScheduledJob, now: datetime, result: JobResult) -> JobResult:
        updated = job.succeeded(at=now, mission_id=result.mission_id, detail=result.detail)
        self._jobs[job.job_id] = updated
        await self._save()
        await self._bus.publish(
            Event(
                type=ev.JOB_SUCCEEDED,
                source="scheduler",
                correlation_id=job.job_id,
                payload={
                    "job_id": job.job_id,
                    "name": job.name,
                    "mission_id": result.mission_id,
                    "next_run_at": updated.next_run_at.isoformat(),
                    "enabled": updated.enabled,
                },
            )
        )
        return result

    async def _fail(self, job: ScheduledJob, now: datetime, result: JobResult) -> JobResult:
        updated = job.failed(at=now, mission_id=result.mission_id, detail=result.detail)
        self._jobs[job.job_id] = updated
        await self._save()

        await self._bus.publish(
            Event(
                type=ev.JOB_FAILED,
                source="scheduler",
                correlation_id=job.job_id,
                priority=Priority.URGENT,
                payload={
                    "job_id": job.job_id,
                    "name": job.name,
                    "detail": result.detail,
                    "attempts": updated.attempts,
                    "max_attempts": updated.max_attempts,
                },
            )
        )

        if updated.enabled:
            await self._bus.publish(
                Event(
                    type=ev.JOB_RETRY_SCHEDULED,
                    source="scheduler",
                    correlation_id=job.job_id,
                    payload={
                        "job_id": job.job_id,
                        "attempt": updated.attempts,
                        "retry_at": updated.next_run_at.isoformat(),
                        "delay_seconds": (updated.next_run_at - now).total_seconds(),
                    },
                )
            )
        else:
            # Giving up is worth saying out loud. A job that quietly stopped
            # retrying is indistinguishable from one that is still trying.
            await self._bus.publish(
                Event(
                    type=ev.JOB_EXHAUSTED,
                    source="scheduler",
                    correlation_id=job.job_id,
                    priority=Priority.URGENT,
                    payload={
                        "job_id": job.job_id,
                        "name": job.name,
                        "attempts": updated.attempts,
                        "detail": result.detail,
                    },
                )
            )
            if self._audit is not None:
                await self._audit.log(
                    action="scheduler.exhausted",
                    actor="scheduler",
                    subject=job.name,
                    decision="disabled",
                    correlation_id=job.job_id,
                    attempts=updated.attempts,
                    detail=result.detail,
                )
        return result

    async def _park(
        self,
        job: ScheduledJob,
        now: datetime,
        *,
        detail: str,
        mission_id: str | None = None,
        refused: bool = False,
    ) -> JobResult:
        updated = job.parked(at=now, mission_id=mission_id, detail=detail)
        self._jobs[job.job_id] = updated
        await self._save()

        await self._bus.publish(
            Event(
                type=ev.JOB_PARKED,
                source="scheduler",
                correlation_id=job.job_id,
                device_id=job.device_id,
                priority=Priority.URGENT,
                payload={
                    "job_id": job.job_id,
                    "name": job.name,
                    "capability": job.capability,
                    "detail": detail,
                    "mission_id": mission_id,
                    "refused_before_running": refused,
                },
            )
        )
        if self._audit is not None:
            await self._audit.log(
                action="scheduler.parked",
                actor="scheduler",
                subject=job.capability or job.name,
                decision="awaiting_confirmation",
                correlation_id=job.job_id,
                detail=detail,
                refused_before_running=refused,
            )
        return JobResult(
            outcome=JobOutcome.REFUSED if refused else JobOutcome.PARKED,
            mission_id=mission_id,
            detail=detail,
        )

    # -- introspection ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        jobs = self.jobs()
        return {
            "running": self._loop is not None,
            "total": len(jobs),
            "enabled": sum(1 for j in jobs if j.enabled),
            "needing_approval": sum(1 for j in jobs if self.needs_approval(j)),
            "max_unattended_level": self._max_unattended_level.code,
            "next_run_at": jobs[0].next_run_at.isoformat() if jobs else None,
            "jobs": [
                {**j.to_dict(), "needs_approval_each_run": self.needs_approval(j)} for j in jobs
            ],
        }


def in_seconds(seconds: float) -> datetime:
    """Small helper for callers scheduling something relative to now."""
    from jarvis.events.envelope import utc_now

    return utc_now() + timedelta(seconds=seconds)
