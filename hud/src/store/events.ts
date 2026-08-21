/**
 * The event store - the seam between the live stream and every component.
 *
 * Components never touch the WebSocket. They read derived state from here,
 * which is what makes Blueprint 3.3's decoupling real rather than aspirational:
 * a slow or reconnecting socket changes `connection`, not the render loop, and
 * a burst of events updates state once per store notification rather than
 * once per event racing the frame clock.
 *
 * `coreActivity` is derived, not stored as its own field the server sets -
 * Blueprint 7.3 warns against a UI that "täuscht Status vor" by inventing
 * animation state. Every value here traces back to a real event or a real API
 * response.
 */

import { create } from "zustand";
import { EventSocket, type ConnectionState } from "../api/ws";
import { api } from "../api/client";
import type { CoreStatus, JarvisEvent, Mission, MissionProgress, PendingApproval } from "../types/api";

export type CoreActivity = "idle" | "listening" | "thinking" | "speaking" | "working";

const MAX_LOG = 200;

interface EventState {
  connection: ConnectionState;
  events: JarvisEvent[];
  status: CoreStatus | null;
  missions: Record<string, Mission>;
  missionOrder: string[];
  progress: Record<string, MissionProgress>;
  activity: CoreActivity;
  speakingDevice: string | null;
  lastSpokenText: string | null;

  connect: () => void;
  refreshStatus: () => Promise<void>;
  loadMissions: () => Promise<void>;
  refreshMission: (id: string) => Promise<void>;
  refreshMissionProgress: (id: string) => Promise<void>;
  sendCommand: (text: string, deviceId?: string) => Promise<void>;
  approve: (approval: PendingApproval) => Promise<void>;
  deny: (fingerprint: string) => Promise<void>;
  resumeMission: (id: string) => Promise<void>;
}

let socket: EventSocket | null = null;

// A burst of mission events (approval granted, then RUNNING, then VERIFYING,
// then COMPLETED) fires several fetches for the same mission in quick
// succession, and the network gives no guarantee they resolve in the order
// they were sent. Without a guard, a slow response for an *earlier* state can
// land after a fast response for a *later* one and silently overwrite it -
// which is exactly how a completed mission ends up showing "0/1 done" on
// screen. Each id gets a monotonic request counter; a response is applied
// only if it is still the most recent request for that id.
const missionRequestSeq = new Map<string, number>();
const progressRequestSeq = new Map<string, number>();

function nextSeq(map: Map<string, number>, id: string): number {
  const seq = (map.get(id) ?? 0) + 1;
  map.set(id, seq);
  return seq;
}

/** Blueprint 3.2's "Jarvis" row: Listening/Thinking/Speaking/Working. */
function activityFor(type: string, current: CoreActivity): CoreActivity {
  switch (type) {
    case "voice.wake.detected":
      return "listening";
    case "voice.transcript.final":
      return "thinking";
    case "voice.speaking.started":
      return "speaking";
    case "voice.speaking.finished":
    case "voice.withheld":
      return "idle";
    case "mission.state.changed":
    case "agent.invoked":
      return "working";
    case "mission.progress":
      return current === "idle" ? "working" : current;
    default:
      return current;
  }
}

function isMissionEvent(type: string): boolean {
  return type.startsWith("mission.");
}

export const useEventStore = create<EventState>((set, get) => ({
  connection: "closed",
  events: [],
  status: null,
  missions: {},
  missionOrder: [],
  progress: {},
  activity: "idle",
  speakingDevice: null,
  lastSpokenText: null,

  connect: () => {
    if (socket) return;
    socket = new EventSocket(
      (event) => {
        set((state) => {
          const next: Partial<EventState> = {
            events: [event, ...state.events].slice(0, MAX_LOG),
            activity: activityFor(event.type, state.activity),
          };
          if (event.type === "voice.speaking.started") {
            next.speakingDevice = event.device_id;
          }
          if (event.type === "voice.speaking.finished") {
            next.speakingDevice = null;
          }
          return next;
        });

        const missionId = event.payload["mission_id"];
        if (isMissionEvent(event.type) && typeof missionId === "string") {
          void get().refreshMission(missionId);
          void get().refreshMissionProgress(missionId);
        }
        if (event.type.startsWith("permission.") || event.type.startsWith("mission.approval")) {
          void get().refreshStatus();
        }
      },
      (connection) => set({ connection }),
    );
    socket.connect();
    void get().refreshStatus();
  },

  refreshStatus: async () => {
    const status = await api.status().catch(() => null);
    if (status) set({ status });
  },

  loadMissions: async () => {
    const list = await api.missions().catch(() => null);
    if (!list) return;
    set((state) => {
      const missions = { ...state.missions };
      const fetchedIds = new Set(list.map((m) => m.mission_id));
      for (const mission of list) missions[mission.mission_id] = mission;
      // Missions already known only via a live event (rarer - e.g. one
      // created after the last fetch) stay listed even if this page didn't
      // include them.
      const carriedOver = state.missionOrder.filter((id) => !fetchedIds.has(id));
      return { missions, missionOrder: [...list.map((m) => m.mission_id), ...carriedOver] };
    });
  },

  refreshMission: async (id: string) => {
    const seq = nextSeq(missionRequestSeq, id);
    const mission = await api.mission(id).catch(() => null);
    if (!mission || missionRequestSeq.get(id) !== seq) return;
    set((state) => ({
      missions: { ...state.missions, [id]: mission },
      missionOrder: state.missionOrder.includes(id)
        ? state.missionOrder
        : [id, ...state.missionOrder],
    }));
  },

  refreshMissionProgress: async (id: string) => {
    const seq = nextSeq(progressRequestSeq, id);
    const progress = await api.missionProgress(id).catch(() => null);
    if (!progress || progressRequestSeq.get(id) !== seq) return;
    set((state) => ({ progress: { ...state.progress, [id]: progress } }));
  },

  sendCommand: async (text: string, deviceId?: string) => {
    const result = await api.command(text, deviceId);
    if (result.mission_id) {
      await get().refreshMission(result.mission_id);
      await get().refreshMissionProgress(result.mission_id);
    }
  },

  approve: async (approval: PendingApproval) => {
    await api.approve(approval.fingerprint, approval.confirmation === "strong");
    await get().refreshStatus();
  },

  deny: async (fingerprint: string) => {
    await api.deny(fingerprint);
    await get().refreshStatus();
  },

  resumeMission: async (id: string) => {
    await api.resumeMission(id);
    await get().refreshMission(id);
    await get().refreshMissionProgress(id);
  },
}));
