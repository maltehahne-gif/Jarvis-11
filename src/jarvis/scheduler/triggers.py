"""Trigger Watcher - Blueprint 8.3's "after" routines.

The learning loop can notice that one action habitually follows another
("nach dem Status-Check schaltest du das Licht aus") and offer it as a
`RoutineProposal` with an `AFTER` trigger. Until now an approved one of
those was a promise the system could not keep: the Scheduler works from a
clock, and no clock can tell it that the preceding action just happened.

This module is the missing half. It watches the Event Bus for completed
actions and asks the Scheduler to fire the routines armed for them.

Three decisions carry the weight:

**It is a queue-backed subscriber, not an inline handler.** `EventBus.on`
handlers run *inside* `publish`, so a routine started from one would make
the owner's own action wait for a whole background mission to finish -
precisely the stall Principle 4 (Fluid-first) forbids. So the watcher takes
a `subscribe()` queue and drains it on its own task, and the action that
triggered it returns immediately.

**It decides only *when*, never *whether*.** Firing goes through
`Scheduler.fire_now`, which applies the same unattended risk ceiling, kill
switch, retry/backoff and audit trail a timed job gets. An event-driven
routine is still unattended execution, so it may do no more than one on a
timer - a P3 action parks for the owner either way.

**A routine's own action cannot trigger a routine.** That is what makes
cycles impossible rather than merely unlikely: routine A firing capability
Y can never set off routine B armed on Y, so no chain can close on itself.
The rule also happens to be the honest reading of the pattern - the habit
was learned by watching the *owner* do one thing after another, so the
owner doing the first thing is the trigger. A deeper chain would need real
cycle detection, and nothing in the blueprint asks for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from jarvis.audit.logger import AuditLogger
from jarvis.events import types as ev
from jarvis.events.bus import EventBus, Subscription
from jarvis.events.envelope import Event
from jarvis.scheduler.jobs import JobResult
from jarvis.scheduler.scheduler import Scheduler

log = logging.getLogger(__name__)

#: Verification statuses that count as "this action really happened". The same
#: standard the memory service learns from - a routine keyed to an action must
#: not fire on one that only claimed to succeed (Blueprint 5.4).
TRIGGERING_VERIFICATION = frozenset({"passed", "unverifiable"})

#: Actors whose actions may set a routine off. Everything the scheduler itself
#: does is excluded, which is the cycle guard described in the module
#: docstring.
BLOCKED_ACTORS = frozenset({"scheduler"})


class TriggerWatcher:
    """Fires `AFTER` routines when the action they follow completes."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        bus: EventBus,
        audit: AuditLogger | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._bus = bus
        self._audit = audit
        self._task: asyncio.Task[None] | None = None
        self._subscription: Subscription | None = None
        self._fired = 0
        self._suppressed = 0

    @property
    def fired(self) -> int:
        """How many routines this watcher has set off."""
        return self._fired

    @property
    def suppressed(self) -> int:
        """Triggers refused because a routine, not the owner, caused them."""
        return self._suppressed

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._subscription = self._bus.subscribe(ev.TOOL_SUCCEEDED, name="trigger-watcher")
        self._task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    async def _drain(self) -> None:
        assert self._subscription is not None
        while True:
            event = await self._subscription.queue.get()
            try:
                await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad routine must not stop the watcher from ever firing
                # another, exactly as one bad job does not stop the Scheduler.
                log.exception("trigger watcher failed on %s", event.type)

    # -- matching -----------------------------------------------------------

    def should_trigger(self, event: Event) -> str | None:
        """The capability this event may trigger routines for, or `None`."""
        if event.type != ev.TOOL_SUCCEEDED:
            return None

        capability = event.payload.get("capability")
        if not isinstance(capability, str) or not capability:
            return None

        if event.payload.get("verification") not in TRIGGERING_VERIFICATION:
            # "Tool aufgerufen" is not "Ziel erreicht" (Blueprint 5.4), and a
            # routine that follows an action ought to follow a real one.
            return None

        # No actor means an older event or a producer that does not record
        # one. Refusing is the safe direction: a missed routine is a
        # disappointment, a routine loop is an incident.
        actor = event.payload.get("actor")
        if not isinstance(actor, str) or actor in BLOCKED_ACTORS:
            self._suppressed += 1
            return None

        return capability

    async def handle(self, event: Event) -> list[JobResult]:
        """Fire every routine armed for this event's capability."""
        capability = self.should_trigger(event)
        if capability is None:
            return []

        armed = self._scheduler.armed_for(capability)
        if not armed:
            return []

        results: list[JobResult] = []
        for job in armed:
            log.info("trigger: %s fired routine %s", capability, job.name)
            result = await self._scheduler.fire_now(job.job_id)
            if result is None:
                continue
            self._fired += 1
            results.append(result)

            if self._audit is not None:
                await self._audit.log(
                    action="scheduler.triggered",
                    actor="trigger-watcher",
                    subject=job.name,
                    decision=str(result.outcome),
                    correlation_id=event.correlation_id,
                    trigger_capability=capability,
                    job_id=job.job_id,
                    origin=job.origin,
                    mission_id=result.mission_id,
                )
        return results
