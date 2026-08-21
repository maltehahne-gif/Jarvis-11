import type { Mission } from "../../types/api";

const STATE_CLASS: Record<string, string> = {
  COMPLETED: "ok",
  FAILED: "danger",
  BLOCKED: "danger",
  CANCELED: "danger",
  WAITING_FOR_APPROVAL: "warn",
  PAUSED: "warn",
};

export function MissionList({
  missions,
  selectedId,
  onSelect,
}: {
  missions: Mission[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (missions.length === 0) {
    return <div className="panel-empty">Noch keine Missionen.</div>;
  }

  return (
    <div className="mission-list">
      {missions.map((mission) => (
        <button
          key={mission.mission_id}
          className={`mission-list__item${mission.mission_id === selectedId ? " is-selected" : ""}`}
          onClick={() => onSelect(mission.mission_id)}
        >
          <span
            className={`mission-list__dot mission-list__dot--${STATE_CLASS[mission.state] ?? "normal"}`}
          />
          <span className="mission-list__goal">{mission.goal}</span>
          <span className="mission-list__state mono">{mission.state}</span>
        </button>
      ))}
    </div>
  );
}
