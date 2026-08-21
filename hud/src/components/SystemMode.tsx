/**
 * System mode - Blueprint 3.2: "PC/Netzwerk/Service-Topologie, Load, Health,
 * Alerts."
 *
 * Every panel reads data the Core already exposes through existing
 * endpoints (`/status`, `/capabilities`, `/scheduler`, `/audit`) - there is
 * no System-mode-specific backend surface. Alerts are derived client-side
 * from those same signals rather than invented; see `AlertsPanel`.
 */

import { useEffect } from "react";
import { useEventStore } from "../store/events";
import { AlertsPanel } from "./system/AlertsPanel";
import { DeviceGrid } from "./system/DeviceGrid";
import { ServiceTable } from "./system/ServiceTable";
import { SchedulerPanel } from "./system/SchedulerPanel";

export function SystemMode() {
  const status = useEventStore((s) => s.status);
  const capabilities = useEventStore((s) => s.capabilities);
  const scheduler = useEventStore((s) => s.scheduler);
  const loadMissions = useEventStore((s) => s.loadMissions);
  const loadCapabilities = useEventStore((s) => s.loadCapabilities);
  const refreshScheduler = useEventStore((s) => s.refreshScheduler);
  const refreshAudit = useEventStore((s) => s.refreshAudit);
  const refreshStatus = useEventStore((s) => s.refreshStatus);

  useEffect(() => {
    void loadMissions();
    void loadCapabilities();
    void refreshScheduler();
    void refreshAudit();
    void refreshStatus();
  }, [loadMissions, loadCapabilities, refreshScheduler, refreshAudit, refreshStatus]);

  const devices = status?.state.devices ?? [];

  return (
    <div className="system-mode">
      <section className="system-mode__alerts">
        <h3>Alerts</h3>
        <AlertsPanel />
      </section>

      <section className="system-mode__devices">
        <h3>Geräte</h3>
        <DeviceGrid devices={devices} />
      </section>

      <section className="system-mode__services">
        <h3>Services</h3>
        <ServiceTable capabilities={capabilities} />
      </section>

      <section className="system-mode__scheduler">
        <h3>Scheduler</h3>
        <SchedulerPanel scheduler={scheduler} />
      </section>
    </div>
  );
}
