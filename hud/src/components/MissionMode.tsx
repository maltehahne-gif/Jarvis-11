/**
 * Mission mode - Blueprint 3.2: "goal tree, agents, dependencies, approvals,
 * checkpoints, ETA/health."
 *
 * Every panel here reads from data the Core already computed and persisted -
 * the Planner's waves, the Mission Runner's checkpoints, the Permission
 * Engine's pending approvals. Nothing is derived beyond the honest
 * arithmetic in `MissionHealth` (an ETA averaged from real checkpoint
 * timestamps). That is the same discipline the debug dashboard follows,
 * carried into the HUD.
 */

import { useEffect, useState } from "react";
import { useEventStore } from "../store/events";
import { MissionList } from "./mission/MissionList";
import { MissionTree } from "./mission/MissionTree";
import { MissionHealth } from "./mission/MissionHealth";
import { ApprovalPanel } from "./mission/ApprovalPanel";
import { CoreOrb } from "./CoreOrb";

export function MissionMode() {
  const activity = useEventStore((s) => s.activity);
  const missions = useEventStore((s) => s.missions);
  const missionOrder = useEventStore((s) => s.missionOrder);
  const progress = useEventStore((s) => s.progress);
  const pendingApprovals = useEventStore((s) => s.status?.pending_approvals ?? []);
  const loadMissions = useEventStore((s) => s.loadMissions);
  const refreshMissionProgress = useEventStore((s) => s.refreshMissionProgress);
  const resumeMission = useEventStore((s) => s.resumeMission);
  const sendCommand = useEventStore((s) => s.sendCommand);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [commandText, setCommandText] = useState("");

  useEffect(() => {
    void loadMissions();
  }, [loadMissions]);

  useEffect(() => {
    if (!selectedId && missionOrder.length > 0) {
      setSelectedId(missionOrder[0]);
    }
  }, [missionOrder, selectedId]);

  useEffect(() => {
    if (selectedId) void refreshMissionProgress(selectedId);
  }, [selectedId, refreshMissionProgress]);

  const missionList = missionOrder.map((id) => missions[id]).filter(Boolean);
  const selected = selectedId ? missions[selectedId] : null;
  const selectedProgress = selectedId ? progress[selectedId] : null;
  const plan = selected?.context.plan;
  const missionApprovals = pendingApprovals.filter(
    (a) => a.mission_id === selectedId || (!selectedId && a.mission_id === null),
  );

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const text = commandText.trim();
    if (!text) return;
    setCommandText("");
    await sendCommand(text);
    await loadMissions();
  }

  return (
    <div className="mission-mode">
      <aside className="mission-mode__sidebar">
        <div className="mission-mode__core">
          <CoreOrb activity={activity} compact />
        </div>
        <form className="mission-mode__command" onSubmit={(e) => void submit(e)}>
          <input
            value={commandText}
            onChange={(e) => setCommandText(e.target.value)}
            placeholder="Ziel eingeben…"
          />
          <button type="submit">senden</button>
        </form>
        <MissionList missions={missionList} selectedId={selectedId} onSelect={setSelectedId} />
      </aside>

      <main className="mission-mode__main">
        {!selected && <div className="panel-empty">Wähle eine Mission.</div>}

        {selected && (
          <>
            <header className="mission-mode__header">
              <h2>{selected.goal}</h2>
              <span className="mission-mode__state mono">{selected.state}</span>
              {selected.state === "PAUSED" && (
                <button className="btn" onClick={() => void resumeMission(selected.mission_id)}>
                  fortsetzen
                </button>
              )}
            </header>

            {selectedProgress && <MissionHealth progress={selectedProgress} />}

            <section className="mission-mode__tree-wrap">
              {plan ? (
                <MissionTree plan={plan} tasks={selected.tasks} />
              ) : (
                <div className="panel-empty">Kein Plan gespeichert.</div>
              )}
            </section>
          </>
        )}
      </main>

      <aside className="mission-mode__approvals">
        <h3>Freigaben</h3>
        <ApprovalPanel approvals={missionApprovals} />
      </aside>
    </div>
  );
}
