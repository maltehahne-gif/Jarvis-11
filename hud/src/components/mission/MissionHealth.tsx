/**
 * Health and ETA - Blueprint 3.2's "checkpoints, ETA/health".
 *
 * The ETA is a real computation over real checkpoint timestamps (average
 * interval between the persisted checkpoints, projected over the remaining
 * task count) - never a fabricated number. With fewer than two checkpoints
 * there is nothing to average, and this shows "–" rather than guess, for the
 * same reason Blueprint 7.3 gives for not inventing UI status: a wrong-looking
 * precise number is worse than an honest absence of one.
 */

import type { Checkpoint, MissionProgress } from "../../types/api";

function estimateRemainingMs(checkpoints: Checkpoint[], remaining: number): number | null {
  if (checkpoints.length < 2 || remaining <= 0) return null;
  const first = new Date(checkpoints[0].at).getTime();
  const last = new Date(checkpoints[checkpoints.length - 1].at).getTime();
  const steps = checkpoints.length - 1;
  if (steps <= 0) return null;
  const avgIntervalMs = (last - first) / steps;
  if (!Number.isFinite(avgIntervalMs) || avgIntervalMs <= 0) return null;
  return avgIntervalMs * remaining;
}

function formatDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `~${seconds}s`;
  return `~${Math.round(seconds / 60)}min`;
}

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELED"]);

export function MissionHealth({ progress }: { progress: MissionProgress }) {
  const remaining = progress.tasks_total - progress.tasks_done - progress.tasks_failed - progress.tasks_skipped;
  const etaMs = TERMINAL.has(progress.state)
    ? null
    : estimateRemainingMs(progress.checkpoints, remaining);

  const healthClass =
    progress.tasks_failed > 0 || progress.state === "FAILED" || progress.state === "BLOCKED"
      ? "danger"
      : progress.state === "WAITING_FOR_APPROVAL" || progress.state === "PAUSED"
        ? "warn"
        : "ok";

  return (
    <div className="mission-health">
      <div className="mission-health__bar">
        <div
          className={`mission-health__fill mission-health__fill--${healthClass}`}
          style={{ width: `${Math.round(progress.fraction_done * 100)}%` }}
        />
      </div>
      <div className="mission-health__stats mono">
        <span>{progress.tasks_done}/{progress.tasks_total} erledigt</span>
        {progress.tasks_failed > 0 && (
          <span className="text-danger">{progress.tasks_failed} fehlgeschlagen</span>
        )}
        {progress.tasks_skipped > 0 && <span>{progress.tasks_skipped} übersprungen</span>}
        <span>{progress.checkpoints.length} ⚑</span>
        <span>ETA {etaMs !== null ? formatDuration(etaMs) : "–"}</span>
      </div>
    </div>
  );
}
