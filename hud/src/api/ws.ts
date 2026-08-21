/**
 * Live event stream - Blueprint 3.3 and the `/ws` endpoint's own contract.
 *
 * The socket forwards only events the Core actually published and persisted
 * (server.py's own docstring: "the surface never invents status of its own").
 * This client keeps that guarantee on the way in too: it does not synthesise
 * events, and a disconnect is shown as a disconnect rather than papered over
 * with the last known state pretending to still be live.
 *
 * Reconnection uses capped exponential backoff so a Core restart is
 * recovered from automatically without hammering the port while it is down.
 */

import { BASE_URL } from "./client";
import type { JarvisEvent } from "../types/api";

export type ConnectionState = "connecting" | "open" | "closed";

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 8_000;

export class EventSocket {
  private socket: WebSocket | null = null;
  private backoff = INITIAL_BACKOFF_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  private readonly onEvent: (event: JarvisEvent) => void;
  private readonly onStateChange: (state: ConnectionState) => void;

  constructor(
    onEvent: (event: JarvisEvent) => void,
    onStateChange: (state: ConnectionState) => void,
  ) {
    this.onEvent = onEvent;
    this.onStateChange = onStateChange;
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  private open(): void {
    if (this.stopped) return;
    this.onStateChange("connecting");

    const url = BASE_URL.replace(/^http/, "ws") + "/ws";
    const socket = new WebSocket(url);
    this.socket = socket;

    socket.onopen = () => {
      this.backoff = INITIAL_BACKOFF_MS;
      this.onStateChange("open");
    };

    socket.onmessage = (message) => {
      try {
        this.onEvent(JSON.parse(message.data as string) as JarvisEvent);
      } catch {
        // A malformed frame is a bug worth ignoring rather than crashing the
        // whole HUD over - the next event is unaffected.
      }
    };

    socket.onclose = () => {
      this.onStateChange("closed");
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      socket.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
      this.open();
    }, this.backoff);
  }

  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }
}
