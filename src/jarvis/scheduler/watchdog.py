"""Watchdog - Blueprint 7.3.

The threat model lists the countermeasures against "Agent-Endlosschleife" as
"time/token/cost budgets, watchdog, checkpoints". Budgets and checkpoints are
in the Execution Gateway and the Mission Runner respectively; this is the third.

The distinction matters, because budgets alone are not enough. The runner
checks the budget *between* steps, which catches a plan that loops. It cannot
catch a single step that never returns - a wedged subprocess, a provider call
that hangs, a process killed between checkpoint and transition. In all of those
the mission simply stays RUNNING forever, and a HUD showing perpetual activity
is worse than one showing a failure: it misreports the system's state, which
Blueprint 7.3 names as its own risk ("UI täuscht Status vor").

So the watchdog watches the clock rather than the work. A mission that has been
RUNNING past its budgeted duration, with no sign of progress, is declared
failed. That verdict may occasionally be wrong about a genuinely slow step -
and that is the right way to be wrong. A mission wrongly marked failed is
visible and re-runnable from its checkpoint; a hung mission left RUNNING is
invisible and blocks its own retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jarvis.audit.logger import AuditLogger
from jarvis.events import types as ev
from jarvis.events.bus import EventBus
from jarvis.events.envelope import Event, Priority, utc_now
from jarvis.mission.engine import MissionEngine
from jarvis.mission.model import Mission, MissionState

log = logging.getLogger(__name__)

#: Added on top of the budgeted duration before a mission is declared hung.
#: Without it, a mission that legitimately used its whole budget would be
#: killed by the watchdog in the same instant the runner was about to stop it
#: cleanly - and the clean stop carries the better explanation.
DEFAULT_GRACE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class WatchdogReport:
    """What one sweep found."""

    checked: int
    tripped: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "tripped": list(self.tripped)}


class Watchdog:
    def __init__(
        self,
        *,
        missions: MissionEngine,
        bus: EventBus,
        max_duration: timedelta,
        audit: AuditLogger | None = None,
        grace: timedelta = DEFAULT_GRACE,
    ) -> None:
        self._missions = missions
        self._bus = bus
        self._max_duration = max_duration
        self._audit = audit
        self._grace = grace

    def is_stale(self, mission: Mission, *, now: datetime | None = None) -> bool:
        """Whether a mission has been RUNNING with nothing happening for too long.

        `updated_at` moves on every transition, task update and checkpoint, so
        a mission making any progress at all keeps resetting this clock. Only
        one that is genuinely stuck goes quiet for the whole window.
        """
        if mission.state is not MissionState.RUNNING:
            return False
        return (now or utc_now()) - mission.updated_at > self._max_duration + self._grace

    async def sweep(self, *, now: datetime | None = None) -> WatchdogReport:
        """Fail every mission that has stopped making progress."""
        moment = now or utc_now()
        running = await self._missions.list(state=MissionState.RUNNING)
        tripped: list[str] = []

        for mission in running:
            if not self.is_stale(mission, now=moment):
                continue

            silent_for = moment - mission.updated_at
            reason = (
                f"watchdog: no progress for {silent_for.total_seconds():.0f}s, "
                f"past the budgeted {self._max_duration.total_seconds():.0f}s"
            )
            await self._missions.transition(mission, MissionState.FAILED, reason)
            tripped.append(mission.mission_id)

            await self._bus.publish(
                Event(
                    type=ev.WATCHDOG_TRIPPED,
                    source="watchdog",
                    correlation_id=mission.correlation_id,
                    device_id=mission.device_id,
                    priority=Priority.URGENT,
                    payload={
                        "mission_id": mission.mission_id,
                        "goal": mission.goal,
                        "silent_seconds": silent_for.total_seconds(),
                        "checkpoints": len(mission.checkpoints),
                        "completed_tasks": len(mission.completed_task_ids),
                    },
                )
            )
            if self._audit is not None:
                await self._audit.log(
                    action="safety.watchdog",
                    actor="watchdog",
                    subject=mission.mission_id,
                    decision="failed",
                    correlation_id=mission.correlation_id,
                    prev_state={"state": str(MissionState.RUNNING)},
                    # A watchdog kill is exactly when resuming matters, so the
                    # rollback point names the last checkpoint.
                    rollback_point=(
                        mission.last_checkpoint.to_dict() if mission.last_checkpoint else None
                    ),
                    reason=reason,
                )
            log.warning("watchdog failed mission %s: %s", mission.mission_id, reason)

        return WatchdogReport(checked=len(running), tripped=tuple(tripped))
