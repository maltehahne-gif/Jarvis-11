"""Latency budget - Blueprint 9.2.

    "Latenz-Budget - Zielwerte, keine Garantie"

That heading decides the whole design. These are targets, so the right
behaviour on a miss is to *report* it, not to fail, retry, or degrade. A budget
that threw would turn a slow moment into a broken one; a budget that stayed
silent would let the system get gradually slower with nobody noticing, which is
how "fluid-first" (Principle 4) quietly stops being true.

So this module measures and tells the truth. The seven checkpoints below are
the seven rows of the blueprint's table, with their stated targets.

The measurements are also what makes the streaming design falsifiable. A
serial chain and a streaming pipeline both eventually produce an answer; only
the timestamps show whether the acknowledgement really fired before
transcription finished.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Checkpoint(StrEnum):
    """The measured points from Blueprint 9.2's table."""

    WAKE_ACK = "wake_ack"
    BARGE_IN_STOP = "barge_in_stop"
    LOCAL_ACTION_DISPATCH = "local_action_dispatch"
    INTENT_CLASSIFICATION = "intent_classification"
    FIRST_TTS_AUDIO = "first_tts_audio"
    HUD_FRAME = "hud_frame"
    MISSION_STATUS_EVENT = "mission_status_event"


#: Targets in milliseconds, taken from the table verbatim. Where the blueprint
#: gives a range ("150-250 ms"), the *upper* bound is the target: the lower one
#: is the aspiration, and measuring against it would report a miss on a result
#: the blueprint calls acceptable.
TARGETS_MS: dict[Checkpoint, float] = {
    Checkpoint.WAKE_ACK: 250.0,
    Checkpoint.BARGE_IN_STOP: 150.0,
    Checkpoint.LOCAL_ACTION_DISPATCH: 300.0,
    Checkpoint.INTENT_CLASSIFICATION: 100.0,
    Checkpoint.FIRST_TTS_AUDIO: 1000.0,
    #: 60 FPS minimum -> one frame every ~16.7 ms.
    Checkpoint.HUD_FRAME: 1000.0 / 60.0,
    #: "spätestens alle 1-3 s, solange Aktivität besteht".
    Checkpoint.MISSION_STATUS_EVENT: 3000.0,
}

#: The aspirational half of the ranges, kept so a report can distinguish
#: "within target" from "as good as we hoped".
STRETCH_MS: dict[Checkpoint, float] = {
    Checkpoint.WAKE_ACK: 150.0,
    Checkpoint.FIRST_TTS_AUDIO: 500.0,
    Checkpoint.HUD_FRAME: 1000.0 / 120.0,
    Checkpoint.MISSION_STATUS_EVENT: 1000.0,
}


@dataclass(frozen=True, slots=True)
class Measurement:
    """One timed checkpoint."""

    checkpoint: Checkpoint
    elapsed_ms: float

    @property
    def target_ms(self) -> float:
        return TARGETS_MS[self.checkpoint]

    @property
    def within_target(self) -> bool:
        return self.elapsed_ms <= self.target_ms

    @property
    def within_stretch(self) -> bool:
        stretch = STRETCH_MS.get(self.checkpoint)
        return stretch is not None and self.elapsed_ms <= stretch

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": str(self.checkpoint),
            "elapsed_ms": round(self.elapsed_ms, 2),
            "target_ms": self.target_ms,
            "within_target": self.within_target,
            "within_stretch": self.within_stretch,
        }


@dataclass(slots=True)
class LatencyTrace:
    """Timings for one voice turn.

    Uses a monotonic clock: wall time can jump backwards, and a negative
    latency would be worse than no measurement.
    """

    started_at: float = field(default_factory=time.monotonic)
    measurements: list[Measurement] = field(default_factory=list)

    def mark(self, checkpoint: Checkpoint, *, since: float | None = None) -> Measurement:
        """Record the time from the turn's start, or from `since`."""
        elapsed = (time.monotonic() - (since if since is not None else self.started_at)) * 1000.0
        measurement = Measurement(checkpoint=checkpoint, elapsed_ms=elapsed)
        self.measurements.append(measurement)
        return measurement

    def record(self, checkpoint: Checkpoint, elapsed_ms: float) -> Measurement:
        """Record a duration measured elsewhere."""
        measurement = Measurement(checkpoint=checkpoint, elapsed_ms=elapsed_ms)
        self.measurements.append(measurement)
        return measurement

    def get(self, checkpoint: Checkpoint) -> Measurement | None:
        return next((m for m in self.measurements if m.checkpoint is checkpoint), None)

    @property
    def misses(self) -> list[Measurement]:
        return [m for m in self.measurements if not m.within_target]

    @property
    def within_budget(self) -> bool:
        return not self.misses

    def to_dict(self) -> dict[str, Any]:
        return {
            "within_budget": self.within_budget,
            "measurements": [m.to_dict() for m in self.measurements],
            "misses": [str(m.checkpoint) for m in self.misses],
        }


class LatencyMonitor:
    """Keeps a rolling record so slow drift is visible, not just one bad turn."""

    def __init__(self, *, window: int = 50) -> None:
        self._window = window
        self._traces: list[LatencyTrace] = []

    def add(self, trace: LatencyTrace) -> LatencyTrace:
        self._traces.append(trace)
        if len(self._traces) > self._window:
            self._traces.pop(0)
        return trace

    def report(self) -> dict[str, Any]:
        """Per-checkpoint medians and miss rates across the window."""
        by_checkpoint: dict[Checkpoint, list[float]] = {}
        for trace in self._traces:
            for measurement in trace.measurements:
                by_checkpoint.setdefault(measurement.checkpoint, []).append(measurement.elapsed_ms)

        rows: list[dict[str, Any]] = []
        for checkpoint, values in sorted(by_checkpoint.items(), key=lambda kv: str(kv[0])):
            ordered = sorted(values)
            median = ordered[len(ordered) // 2]
            target = TARGETS_MS[checkpoint]
            rows.append(
                {
                    "checkpoint": str(checkpoint),
                    "samples": len(ordered),
                    "median_ms": round(median, 2),
                    "worst_ms": round(ordered[-1], 2),
                    "target_ms": target,
                    "miss_rate": round(sum(1 for v in ordered if v > target) / len(ordered), 3),
                }
            )

        return {
            "turns": len(self._traces),
            "window": self._window,
            "checkpoints": rows,
            "targets_are_goals_not_guarantees": True,
        }
