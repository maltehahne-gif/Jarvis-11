"""Scheduled job model - Blueprint 5.1.

    "Zeitbasierte Jobs, Background Missions, Retry/Backoff."

A job is a standing instruction to run something when the owner is not
watching. That single fact shapes every field here:

* `grants` are recorded once, at registration, and never widened at fire time.
  A job cannot acquire rights it was not given.
* `attempts` and `backoff_base` implement the blueprint's Retry/Backoff, with
  a hard `max_attempts` so a permanently broken job stops asking rather than
  retrying forever.
* `origin` records who asked for the job. A routine the learning loop proposed
  and the owner approved is traceable back to that decision (Blueprint 8.3).

Parking is not failing. When a job's action needs confirmation, nobody is
there to give it, so the run parks and waits - it has not gone wrong, and
retrying it on a backoff would be the wrong response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any, Self

from jarvis.events.envelope import new_id, utc_now


class JobKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    DAILY = "daily"
    #: Fires when another capability completes, not on a clock (Blueprint
    #: 8.3's "after" trigger). The clock never makes one of these due; the
    #: Trigger Watcher fires it directly. `next_run_at` is therefore not a
    #: schedule for an AFTER job and must not be shown as one.
    AFTER = "after"


class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The action needs owner confirmation, which an unattended run cannot give.
    PARKED = "parked"
    #: The scheduler declined to run it at all.
    REFUSED = "refused"


#: Exponential backoff, capped. Blueprint 5.1 asks for backoff; the cap keeps a
#: job that fails all night from drifting to a retry interval measured in days.
DEFAULT_BACKOFF_BASE = timedelta(minutes=1)
MAX_BACKOFF = timedelta(hours=6)
DEFAULT_MAX_ATTEMPTS = 3

_HOUR_BUCKET = re.compile(r"^(\d{1,2})h$")


def parse_daily_at(value: str) -> time:
    """Accept `"20h"` (the learning loop's hour bucket) or `"20:30"`."""
    match = _HOUR_BUCKET.match(value.strip())
    if match:
        return time(hour=int(match.group(1)) % 24)
    hours, _, minutes = value.strip().partition(":")
    return time(hour=int(hours) % 24, minute=int(minutes or 0) % 60)


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One standing instruction."""

    name: str
    goal: str
    kind: JobKind = JobKind.ONCE
    capability: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    grants: frozenset[str] = frozenset()
    device_id: str | None = None

    next_run_at: datetime = field(default_factory=utc_now)
    interval: timedelta | None = None
    daily_at: time | None = None
    #: For `JobKind.AFTER`: the capability whose completion arms this job.
    after_capability: str | None = None

    enabled: bool = True
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base: timedelta = DEFAULT_BACKOFF_BASE

    last_run_at: datetime | None = None
    last_outcome: JobOutcome | None = None
    last_detail: str = ""
    last_mission_id: str | None = None
    runs: int = 0

    origin: str = "owner"
    job_id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    # -- scheduling ---------------------------------------------------------

    def is_due(self, now: datetime | None = None) -> bool:
        # An event-driven job is never due by the clock. Letting the poll loop
        # pick one up would fire it at an arbitrary moment that has nothing to
        # do with its trigger - the opposite of what "after X" promises.
        if self.kind is JobKind.AFTER:
            return False
        return self.enabled and (now or utc_now()) >= self.next_run_at

    def next_occurrence(self, after: datetime) -> datetime | None:
        """When this job should run again, or `None` if it is finished."""
        if self.kind is JobKind.ONCE:
            return None
        if self.kind is JobKind.AFTER:
            # Not "finished" and not scheduled either: it stays armed for the
            # next occurrence of its trigger. Returning a non-None value is
            # what keeps `succeeded()` and `parked()` from disabling it, and
            # the value itself is never read, because `is_due` ignores it.
            return self.next_run_at
        if self.kind is JobKind.INTERVAL:
            interval = self.interval or timedelta(hours=1)
            # Skip past any missed slots rather than firing a burst to catch
            # up: a machine that was asleep for six hours should resume the
            # rhythm, not run six times in a row.
            nxt = self.next_run_at + interval
            while nxt <= after:
                nxt += interval
            return nxt
        at = self.daily_at or time(hour=9)
        candidate = datetime.combine(after.date(), at, tzinfo=after.tzinfo)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    def backoff_delay(self) -> timedelta:
        """Exponential in the attempt count, capped."""
        delay = self.backoff_base * (2 ** max(self.attempts - 1, 0))
        return min(delay, MAX_BACKOFF)

    # -- outcome transitions ------------------------------------------------

    def succeeded(self, *, at: datetime, mission_id: str | None, detail: str = "") -> Self:
        """A clean run resets the retry counter."""
        nxt = self.next_occurrence(at)
        return replace(
            self,
            attempts=0,
            runs=self.runs + 1,
            last_run_at=at,
            last_outcome=JobOutcome.SUCCEEDED,
            last_detail=detail,
            last_mission_id=mission_id,
            next_run_at=nxt or self.next_run_at,
            enabled=nxt is not None,
        )

    def failed(self, *, at: datetime, mission_id: str | None, detail: str = "") -> Self:
        """Schedule a backoff retry, or give up after `max_attempts`."""
        attempts = self.attempts + 1
        exhausted = attempts >= self.max_attempts
        candidate = replace(self, attempts=attempts)
        retry_at = at + candidate.backoff_delay()

        return replace(
            self,
            attempts=attempts,
            runs=self.runs + 1,
            last_run_at=at,
            last_outcome=JobOutcome.FAILED,
            last_detail=detail,
            last_mission_id=mission_id,
            next_run_at=retry_at,
            enabled=not exhausted,
        )

    def parked(self, *, at: datetime, mission_id: str | None, detail: str = "") -> Self:
        """Waiting for the owner is not a failure and earns no backoff.

        The job keeps its normal schedule; the parked mission is what needs
        attention, and it is already visible in the approvals queue.
        """
        nxt = self.next_occurrence(at)
        return replace(
            self,
            runs=self.runs + 1,
            last_run_at=at,
            last_outcome=JobOutcome.PARKED,
            last_detail=detail,
            last_mission_id=mission_id,
            next_run_at=nxt or self.next_run_at,
            enabled=nxt is not None,
        )

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "goal": self.goal,
            "kind": str(self.kind),
            "capability": self.capability,
            "params": self.params,
            "grants": sorted(self.grants),
            "device_id": self.device_id,
            "next_run_at": self.next_run_at.isoformat(),
            "interval_seconds": self.interval.total_seconds() if self.interval else None,
            "daily_at": self.daily_at.isoformat() if self.daily_at else None,
            "after_capability": self.after_capability,
            "enabled": self.enabled,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "backoff_base_seconds": self.backoff_base.total_seconds(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_outcome": str(self.last_outcome) if self.last_outcome else None,
            "last_detail": self.last_detail,
            "last_mission_id": self.last_mission_id,
            "runs": self.runs,
            "origin": self.origin,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            job_id=data["job_id"],
            name=data["name"],
            goal=data["goal"],
            kind=JobKind(data["kind"]),
            capability=data.get("capability"),
            params=data.get("params", {}),
            grants=frozenset(data.get("grants", [])),
            device_id=data.get("device_id"),
            next_run_at=datetime.fromisoformat(data["next_run_at"]),
            interval=(
                timedelta(seconds=data["interval_seconds"])
                if data.get("interval_seconds")
                else None
            ),
            daily_at=time.fromisoformat(data["daily_at"]) if data.get("daily_at") else None,
            after_capability=data.get("after_capability"),
            enabled=data.get("enabled", True),
            attempts=data.get("attempts", 0),
            max_attempts=data.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            backoff_base=timedelta(seconds=data.get("backoff_base_seconds", 60)),
            last_run_at=(
                datetime.fromisoformat(data["last_run_at"]) if data.get("last_run_at") else None
            ),
            last_outcome=(JobOutcome(data["last_outcome"]) if data.get("last_outcome") else None),
            last_detail=data.get("last_detail", ""),
            last_mission_id=data.get("last_mission_id"),
            runs=data.get("runs", 0),
            origin=data.get("origin", "owner"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class JobResult:
    """What one firing achieved."""

    outcome: JobOutcome
    mission_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "mission_id": self.mission_id,
            "detail": self.detail,
        }
