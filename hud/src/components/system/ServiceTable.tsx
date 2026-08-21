/**
 * Services - registered Capabilities shown as the system's service table.
 *
 * `level` and `health` come straight from the Capability Registry (the same
 * source the Permission Engine gates against), so a capability that is
 * `degraded` or `unavailable` here is the same fact a mission would hit if
 * it tried to use it.
 */

import type { Capability } from "../../types/api";

interface Props {
  capabilities: Capability[];
}

export function ServiceTable({ capabilities }: Props) {
  if (capabilities.length === 0) {
    return <div className="panel-empty">Keine Capabilities registriert.</div>;
  }

  return (
    <table className="service-table">
      <thead>
        <tr>
          <th>Service</th>
          <th>Level</th>
          <th>Health</th>
          <th>Eigenschaften</th>
        </tr>
      </thead>
      <tbody>
        {capabilities.map((c) => (
          <tr key={c.name}>
            <td className="service-table__name mono">{c.name}</td>
            <td className="service-table__level">{c.level_label}</td>
            <td>
              <span className={`service-table__health service-table__health--${c.health}`}>
                {c.health}
              </span>
            </td>
            <td className="service-table__flags">
              {c.reversible && <span className="flag-pill">reversibel</span>}
              {c.verifiable && <span className="flag-pill">verifizierbar</span>}
              {c.undoable && <span className="flag-pill">undo</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
