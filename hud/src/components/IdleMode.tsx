/**
 * Idle mode - Blueprint 3.2's own words: "Minimaler Core / Ambient mode -
 * keine unnötigen Panels."
 *
 * This is deliberately the smallest component in the HUD. No mission list,
 * no capability table, no memory panel - just the orb, the wake phrase, and a
 * clock. Every other mode earns its panels by having a reason to be on
 * screen right now; idle's reason not to have them is the same sentence.
 */

import { useEffect, useState } from "react";
import { CoreOrb } from "./CoreOrb";
import type { CoreActivity } from "../store/events";

function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function IdleMode({ activity }: { activity: CoreActivity }) {
  const time = useClock();

  return (
    <div className="idle-mode">
      <CoreOrb activity={activity} />
      <div className="idle-mode__clock mono">{time}</div>
      <div className="idle-mode__hint">„Jarvis" — jederzeit</div>
    </div>
  );
}
