/**
 * The one property in the store worth pinning down with a test: a stale
 * network response must never overwrite a fresher one.
 *
 * A burst of mission events (approval granted, then RUNNING, then VERIFYING,
 * then COMPLETED) fires several fetches for the same mission back to back,
 * and nothing guarantees they resolve in the order they were sent. Without
 * the sequence guard in `events.ts`, a slow response for an earlier state
 * landing after a fast response for a later one silently overwrites it -
 * which is exactly the "0/1 done" on a COMPLETED mission bug this test
 * would have caught before it reached a screenshot.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import type { Mission, MissionProgress } from "../types/api";

vi.mock("../api/client", () => ({
  api: {
    mission: vi.fn(),
    missionProgress: vi.fn(),
    status: vi.fn().mockResolvedValue(null),
    missions: vi.fn().mockResolvedValue([]),
    command: vi.fn(),
    approve: vi.fn(),
    deny: vi.fn(),
    resumeMission: vi.fn(),
  },
}));

vi.mock("../api/ws", () => ({
  EventSocket: vi.fn(),
}));

const { api } = await import("../api/client");
const { useEventStore } = await import("./events");

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function progressAt(fraction: number, done: number, total: number): MissionProgress {
  return {
    mission_id: "m1",
    goal: "test",
    state: fraction === 1 ? "COMPLETED" : "RUNNING",
    tasks_total: total,
    tasks_done: done,
    tasks_failed: 0,
    tasks_skipped: 0,
    fraction_done: fraction,
    checkpoints: [],
    plan: null,
  };
}

beforeEach(() => {
  useEventStore.setState({ missions: {}, missionOrder: [], progress: {} });
  vi.mocked(api.missionProgress).mockReset();
  vi.mocked(api.mission).mockReset();
});

describe("refreshMissionProgress ordering", () => {
  it("keeps the newer response when an older request resolves later", async () => {
    const older = deferred<MissionProgress>();
    const newer = deferred<MissionProgress>();
    vi.mocked(api.missionProgress).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

    const store = useEventStore.getState();
    const oldCall = store.refreshMissionProgress("m1"); // request #1 (RUNNING)
    const newCall = store.refreshMissionProgress("m1"); // request #2 (COMPLETED)

    // The newer request's response arrives first...
    newer.resolve(progressAt(1, 1, 1));
    await newCall;
    expect(useEventStore.getState().progress["m1"].tasks_done).toBe(1);

    // ...and the older, now-stale request resolves after it. It must not
    // roll the store back to "0/1".
    older.resolve(progressAt(0, 0, 1));
    await oldCall;

    expect(useEventStore.getState().progress["m1"].tasks_done).toBe(1);
    expect(useEventStore.getState().progress["m1"].state).toBe("COMPLETED");
  });

  it("applies a single response normally", async () => {
    vi.mocked(api.missionProgress).mockResolvedValueOnce(progressAt(0.5, 1, 2));
    await useEventStore.getState().refreshMissionProgress("m1");
    expect(useEventStore.getState().progress["m1"].tasks_done).toBe(1);
  });
});

describe("refreshMission ordering", () => {
  function missionAt(state: Mission["state"]): Mission {
    return {
      mission_id: "m1",
      correlation_id: "c1",
      goal: "test",
      state,
      tasks: [],
      device_id: null,
      user_id: "owner",
      context: {},
      history: [],
      checkpoints: [],
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
  }

  it("keeps the newer mission snapshot when an older request resolves later", async () => {
    const older = deferred<Mission>();
    const newer = deferred<Mission>();
    vi.mocked(api.mission).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

    const store = useEventStore.getState();
    const oldCall = store.refreshMission("m1");
    const newCall = store.refreshMission("m1");

    newer.resolve(missionAt("COMPLETED"));
    await newCall;
    older.resolve(missionAt("RUNNING"));
    await oldCall;

    expect(useEventStore.getState().missions["m1"].state).toBe("COMPLETED");
  });
});
