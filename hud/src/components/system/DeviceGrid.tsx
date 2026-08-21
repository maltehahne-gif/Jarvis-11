/**
 * Device topology - the "PC/Netzwerk" half of Blueprint 3.2's System mode.
 *
 * Reads `status.state.devices` verbatim; online/trusted are the only two
 * facts the Core tracks per device, so those are the only two shown.
 */

import type { CoreStatus } from "../../types/api";

interface Props {
  devices: CoreStatus["state"]["devices"];
}

export function DeviceGrid({ devices }: Props) {
  if (devices.length === 0) {
    return <div className="panel-empty">Keine Geräte bekannt.</div>;
  }

  return (
    <div className="device-grid">
      {devices.map((d) => (
        <div key={d.device_id} className="device-card" data-online={d.online}>
          <span className={`device-card__dot${d.online ? " is-online" : ""}`} />
          <span className="device-card__id mono">{d.device_id}</span>
          <span className="device-card__trust">{d.trusted ? "vertraut" : "nicht vertraut"}</span>
        </div>
      ))}
    </div>
  );
}
