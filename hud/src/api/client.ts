/**
 * REST client for the Core's local API.
 *
 * One rule governs every call here: nothing in the render loop may await one
 * of these directly. Blueprint 3.3 forbids animation, input or scrolling from
 * blocking on a Claude request, and a fetch to `/plan` or `/command` can hit
 * exactly that path. Callers dispatch through the store's actions, which fire
 * a request and let the UI keep animating while it resolves - see
 * `store/events.ts`.
 */

import type {
  AuditSnapshot,
  Capability,
  CoreStatus,
  Mission,
  MissionProgress,
  PendingApproval,
  SchedulerSnapshot,
} from "../types/api";

const BASE_URL = import.meta.env.VITE_JARVIS_API ?? "http://127.0.0.1:8765";

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  status: () => request<CoreStatus>("/status"),
  missions: (limit = 50) => request<Mission[]>(`/missions?limit=${limit}`),
  mission: (id: string) => request<Mission>(`/missions/${id}`),
  missionProgress: (id: string) => request<MissionProgress>(`/missions/${id}/progress`),
  resumeMission: (id: string) =>
    request<{ mission_state: string | null }>(`/missions/${id}/resume`, { method: "POST" }),

  command: (text: string, deviceId?: string, grants: string[] = []) =>
    request<{ mission_id: string | null }>("/command", {
      method: "POST",
      body: JSON.stringify({ text, device_id: deviceId, grants }),
    }),

  approve: (fingerprint: string, strong: boolean, deviceId?: string) =>
    request("/approve", {
      method: "POST",
      body: JSON.stringify({ fingerprint, strong, device_id: deviceId }),
    }),

  deny: (fingerprint: string) =>
    request("/deny", {
      method: "POST",
      body: JSON.stringify({ fingerprint }),
    }),

  capabilities: () => request<Capability[]>("/capabilities"),
  scheduler: () => request<SchedulerSnapshot>("/scheduler"),
  audit: (limit = 100) => request<AuditSnapshot>(`/audit?limit=${limit}`),
};

export type { PendingApproval };
export { ApiError, BASE_URL };
