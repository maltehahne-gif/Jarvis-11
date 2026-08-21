/**
 * Approvals - Blueprint 3.2's Mission row and 7.1's confirmation table.
 *
 * This panel only ever shows what `/status` reports as pending, and Approve
 * only ever calls `/approve` - the decision is made by the Permission Engine
 * on the backend exactly as it would be from the debug dashboard or a typed
 * command. The HUD adds no authority of its own (the same rule Blueprint 7.2
 * applies to voice).
 */

import type { PendingApproval } from "../../types/api";
import { useEventStore } from "../../store/events";

export function ApprovalPanel({ approvals }: { approvals: PendingApproval[] }) {
  const approve = useEventStore((s) => s.approve);
  const deny = useEventStore((s) => s.deny);

  if (approvals.length === 0) {
    return <div className="panel-empty">Keine offenen Freigaben.</div>;
  }

  return (
    <div className="approval-panel">
      {approvals.map((approval) => (
        <div className="approval-card" key={approval.fingerprint}>
          <div className="approval-card__capability mono">{approval.capability}</div>
          <div className="approval-card__params mono">{JSON.stringify(approval.params)}</div>
          <div className="approval-card__confirmation">
            Stufe: <span className="mono">{approval.confirmation}</span>
          </div>
          <div className="approval-card__actions">
            <button className="btn btn--ok" onClick={() => void approve(approval)}>
              freigeben
            </button>
            <button className="btn btn--danger" onClick={() => void deny(approval.fingerprint)}>
              ablehnen
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
