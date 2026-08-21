/**
 * Debug overlay - proof, not decoration.
 *
 * Blueprint 3.3 states a target (60 FPS minimum, 120 on suitable hardware) and
 * Blueprint 9.2 states latency targets elsewhere as "Zielwerte, keine
 * Garantie". This overlay is what turns "should be 60 FPS" into a number you
 * can actually read while the HUD is running, and it is built from
 * `useAnimationFrame` itself - if the render loop were secretly blocked on a
 * fetch, this counter would show it by dropping, not lie by staying frozen at
 * 60.
 */

import { useRef, useState } from "react";
import { useAnimationFrame } from "../hooks/useAnimationFrame";
import { useEventStore } from "../store/events";

const SAMPLE_WINDOW_MS = 500;

export function DebugOverlay() {
  const connection = useEventStore((s) => s.connection);
  const eventCount = useEventStore((s) => s.events.length);
  const [fps, setFps] = useState(0);
  const frames = useRef(0);
  // `null` until the first animation-frame tick sets it - `performance.now()`
  // is an impure read, so it belongs in that callback, not in the ref's
  // render-time initial value.
  const windowStart = useRef<number | null>(null);

  useAnimationFrame(() => {
    const now = performance.now();
    windowStart.current ??= now;
    frames.current += 1;
    const elapsed = now - windowStart.current;
    if (elapsed >= SAMPLE_WINDOW_MS) {
      setFps(Math.round((frames.current * 1000) / elapsed));
      frames.current = 0;
      windowStart.current = now;
    }
  });

  const dotColor =
    connection === "open" ? "var(--ok)" : connection === "connecting" ? "var(--warn)" : "var(--danger)";

  return (
    <div className="debug-overlay mono">
      <span className="debug-dot" style={{ background: dotColor }} aria-hidden />
      <span>{connection}</span>
      <span className="debug-sep">·</span>
      <span style={{ color: fps >= 60 ? "var(--ok)" : fps >= 30 ? "var(--warn)" : "var(--danger)" }}>
        {fps} fps
      </span>
      <span className="debug-sep">·</span>
      <span>{eventCount} events</span>
    </div>
  );
}
