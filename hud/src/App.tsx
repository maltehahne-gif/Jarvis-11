/**
 * The app shell - mode switching per Blueprint 3.2.
 *
 * Only the three modes built so far (Idle, Mission, System) are wired up;
 * the rest of the table (News, Coding, Smart Home, Research) are later
 * increments needing their own data sources. Adding one means adding a case
 * here and a component next to `MissionMode.tsx` - the socket, the store and
 * the debug overlay are already shared infrastructure.
 */

import { useEffect, useState } from "react";
import { useEventStore } from "./store/events";
import { IdleMode } from "./components/IdleMode";
import { MissionMode } from "./components/MissionMode";
import { SystemMode } from "./components/SystemMode";
import { DebugOverlay } from "./components/DebugOverlay";

type Mode = "idle" | "mission" | "system";

const MODES: { id: Mode; label: string }[] = [
  { id: "idle", label: "Idle" },
  { id: "mission", label: "Mission" },
  { id: "system", label: "System" },
];

export default function App() {
  const connect = useEventStore((s) => s.connect);
  const activity = useEventStore((s) => s.activity);
  const [mode, setMode] = useState<Mode>("idle");

  useEffect(() => {
    connect();
  }, [connect]);

  // Working activity (an agent run, a mission progressing) is worth
  // surfacing even if the owner is looking at Idle - it is real backend
  // state, not a nudge to switch modes on their behalf.
  useEffect(() => {
    if (activity === "working" && mode === "idle") setMode("mission");
  }, [activity, mode]);

  return (
    <div className="app-shell">
      <div className="grid-backdrop" aria-hidden />
      <nav className="mode-switcher">
        {MODES.map((m) => (
          <button
            key={m.id}
            className={`mode-switcher__btn${mode === m.id ? " is-active" : ""}`}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </nav>

      <div className="app-shell__content">
        {mode === "idle" && <IdleMode activity={activity} />}
        {mode === "mission" && <MissionMode />}
        {mode === "system" && <SystemMode />}
      </div>

      <DebugOverlay />
    </div>
  );
}
