/**
 * Alerts - Blueprint 3.2's System mode, "Load, Health, Alerts".
 *
 * Every alert here is derived from a real signal already reported by the
 * Core: the kill switch, a broken audit chain, a watchdog trip recorded in
 * the audit log, a scheduler job that failed or ran out of retries, or a
 * mission stuck FAILED/BLOCKED. Nothing is synthesised - an idle system
 * with no matching signal shows an empty list, not a placeholder "all
 * good" that could turn out to be wrong (Blueprint 7.3: "UI täuscht Status
 * vor").
 */

import { useMemo } from "react";
import { useEventStore } from "../../store/events";

type Severity = "warn" | "danger";

interface Alert {
  id: string;
  severity: Severity;
  text: string;
}

export function AlertsPanel() {
  const status = useEventStore((s) => s.status);
  const scheduler = useEventStore((s) => s.scheduler);
  const audit = useEventStore((s) => s.audit);
  const missions = useEventStore((s) => s.missions);
  const missionOrder = useEventStore((s) => s.missionOrder);

  const alerts = useMemo<Alert[]>(() => {
    const list: Alert[] = [];

    if (status?.kill_switch) {
      list.push({
        id: "kill-switch",
        severity: "danger",
        text: status.kill_switch_reason
          ? `Kill Switch aktiv: ${status.kill_switch_reason}`
          : "Kill Switch aktiv",
      });
    }

    if (audit && !audit.chain_valid) {
      list.push({
        id: "audit-chain",
        severity: "danger",
        text: audit.first_broken_entry
          ? `Audit-Kette gebrochen bei Eintrag ${audit.first_broken_entry}`
          : "Audit-Kette gebrochen",
      });
    }

    for (const entry of audit?.entries ?? []) {
      if (entry.action === "safety.watchdog") {
        list.push({
          id: `watchdog-${entry.entry_id}`,
          severity: "danger",
          text: `Watchdog: Mission ${entry.subject} ohne Fortschritt beendet`,
        });
      }
    }

    for (const job of scheduler?.jobs ?? []) {
      if (job.last_outcome === "failed" && !job.enabled) {
        list.push({
          id: `job-exhausted-${job.job_id}`,
          severity: "danger",
          text: `Job "${job.name}" nach ${job.attempts} Versuchen ausgesetzt`,
        });
      } else if (job.last_outcome === "failed") {
        list.push({
          id: `job-failed-${job.job_id}`,
          severity: "warn",
          text: `Job "${job.name}" zuletzt fehlgeschlagen`,
        });
      }
    }

    for (const id of missionOrder) {
      const mission = missions[id];
      if (!mission) continue;
      if (mission.state === "FAILED" || mission.state === "BLOCKED") {
        list.push({
          id: `mission-${mission.mission_id}`,
          severity: mission.state === "FAILED" ? "danger" : "warn",
          text: `Mission "${mission.goal}" ist ${mission.state}`,
        });
      }
    }

    return list;
  }, [status, scheduler, audit, missions, missionOrder]);

  if (alerts.length === 0) {
    return <div className="panel-empty">Keine aktiven Alerts.</div>;
  }

  return (
    <ul className="alerts-panel">
      {alerts.map((a) => (
        <li key={a.id} className={`alerts-panel__item alerts-panel__item--${a.severity}`}>
          <span className="alerts-panel__dot" />
          {a.text}
        </li>
      ))}
    </ul>
  );
}
