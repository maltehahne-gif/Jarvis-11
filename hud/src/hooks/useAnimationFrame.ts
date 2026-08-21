/**
 * A render loop that never touches the network.
 *
 * Blueprint 3.3: "Keine Claude-Anfrage darf Animation, Eingabe, Scrollen,
 * Globe-Rendering oder Wake-Feedback blockieren." This hook is the mechanism
 * that keeps that true - it drives `requestAnimationFrame` from a callback
 * that only ever reads already-fetched state out of the store, never awaits
 * anything. A component animating off this hook cannot stall no matter how
 * slow the Core's next response is, because the loop and the network are two
 * separate call stacks that only meet at a store read.
 */

import { useEffect, useRef } from "react";

export function useAnimationFrame(callback: (deltaMs: number, elapsedMs: number) => void): void {
  const callbackRef = useRef(callback);

  // Keep the ref fresh without writing to it during render - the effect runs
  // after commit, and `tick` (below) only ever reads the ref from inside the
  // browser's animation-frame callback, never during render either.
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const start = last;

    const tick = (now: number) => {
      callbackRef.current(now - last, now - start);
      last = now;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => cancelAnimationFrame(raf);
  }, []);
}
