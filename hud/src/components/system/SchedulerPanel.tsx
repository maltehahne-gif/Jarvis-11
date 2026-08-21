/**
 * Scheduler panel - background job load and health (Blueprint 3.2).
 *
 * `snapshot()` already carries its own summary counts (total, enabled,
 * needing_approval) computed by the Scheduler itself, so the header line
 * repeats those rather than re-deriving them from the job list.
 */

import type { ScheduledJobView, SchedulerSnapshot } from "../../types/api";

interface Props {
  scheduler: SchedulerSnapshot | null;
}

function formatRelative(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  const abs = Math.abs(ms);
  const minutes = Math.round(abs / 60_000);
  const label = minutes < 1 ? "<1 min" : minutes < 60 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
  return ms >= 0 ? `in ${label}` : `vor ${label}`;
}

/**
 * An `after` job has no next run time - it waits for another capability to
 * complete. Its `next_run_at` is a leftover field, so showing it as a
 * countdown would put a time on screen that nothing will happen at.
 */
function describeSchedule(job: ScheduledJobView): string {
  if (!job.enabled) return "pausiert";
  if (job.kind === "after") return `nach ${job.after_capability ?? "?"}`;
  return formatRelative(job.next_run_at);
}

export function SchedulerPanel({ scheduler }: Props) {
  if (!scheduler || scheduler.jobs.length === 0) {
    return <div className="panel-empty">Keine geplanten Jobs.</div>;
  }

  return (
    <div className="scheduler-panel">
      <div className="scheduler-panel__summary mono">
        {scheduler.enabled}/{scheduler.total} aktiv · {scheduler.needing_approval} brauchen Freigabe
        {scheduler.armed > 0 && ` · ${scheduler.armed} ereignisgesteuert`}
      </div>
      <ul className="scheduler-panel__list">
        {scheduler.jobs.map((job) => (
          <li key={job.job_id} className="scheduler-job" data-enabled={job.enabled}>
            <div className="scheduler-job__row">
              <span className="scheduler-job__name">{job.name}</span>
              {job.last_outcome && (
                <span className={`scheduler-job__outcome scheduler-job__outcome--${job.last_outcome}`}>
                  {job.last_outcome}
                </span>
              )}
            </div>
            <div className="scheduler-job__meta mono">
              {describeSchedule(job)}
              {" · "}
              {job.runs} Läufe
              {job.needs_approval_each_run && " · braucht Freigabe"}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
