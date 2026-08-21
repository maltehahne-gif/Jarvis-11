/**
 * Central AI Core - Blueprint 3.2's "Jarvis" row and 3.1's orbit motif.
 *
 * "Central AI Core: Listening/Thinking/Speaking/Working." Four states, one
 * object, no separate panel per state - Blueprint 2.1 describes JARVIS
 * materialising as this one focal point, not as a different screen for each
 * mode. Idle is a fifth, implicit state: the ring at rest.
 *
 * All motion here is CSS (`animation-duration`, transforms), driven by a
 * `data-activity` attribute rather than by per-frame JavaScript. That keeps
 * the orb's animation on the compositor thread, which is what makes it
 * immune to a busy main thread - exactly the guarantee Blueprint 3.3 asks
 * for structurally, not just as a target to hit.
 */

import type { CoreActivity } from "../store/events";
import "./CoreOrb.css";

const LABEL: Record<CoreActivity, string> = {
  idle: "IDLE",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
  working: "WORKING",
};

export function CoreOrb({ activity, compact = false }: { activity: CoreActivity; compact?: boolean }) {
  return (
    <div className={`core-orb${compact ? " core-orb--compact" : ""}`} data-activity={activity}>
      <svg viewBox="0 0 200 200" className="core-orb__rings" aria-hidden>
        <circle className="core-orb__ring core-orb__ring--outer" cx="100" cy="100" r="92" />
        <circle className="core-orb__ring core-orb__ring--mid" cx="100" cy="100" r="72" />
        <circle className="core-orb__ring core-orb__ring--inner" cx="100" cy="100" r="54" />
        {/* Radar sweep - Blueprint 3.1's "kreisförmige Radar-Motive". Only
            animated while there is something to sweep for. */}
        <line className="core-orb__sweep" x1="100" y1="100" x2="100" y2="12" />
      </svg>
      <div className="core-orb__core" />
      {!compact && <div className="core-orb__label mono">{LABEL[activity]}</div>}
    </div>
  );
}
