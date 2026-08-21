/**
 * Types mirroring the JSON the Core actually emits.
 *
 * Kept in exact correspondence with the `to_dict()` methods on the Python
 * side (event envelope, mission model, plan model) rather than reshaped for
 * frontend convenience. A mismatch here is a silent bug the type checker
 * cannot catch once it exists, so the source of truth stays the backend and
 * this file follows it - not the other way round.
 */

export type Sensitivity = "public" | "private" | "secret";
export type Priority = "background" | "normal" | "urgent" | "critical";

/** Blueprint 5.2's Event Envelope. Every live update arrives as one of these. */
export interface JarvisEvent {
  event_id: string;
  type: string;
  timestamp: string;
  source: string;
  correlation_id: string;
  user_id: string;
  device_id: string | null;
  sensitivity: Sensitivity;
  priority: Priority;
  payload: Record<string, unknown>;
  ttl: number | null;
}

export type MissionState =
  | "CREATED"
  | "PLANNING"
  | "WAITING_FOR_APPROVAL"
  | "RUNNING"
  | "VERIFYING"
  | "COMPLETED"
  | "PAUSED"
  | "BLOCKED"
  | "FAILED"
  | "CANCELED";

export type TaskState = "PENDING" | "RUNNING" | "DONE" | "FAILED" | "SKIPPED";

export interface JarvisTask {
  task_id: string;
  description: string;
  capability: string | null;
  params: Record<string, unknown>;
  state: TaskState;
  depends_on: string[];
  result: Record<string, unknown> | null;
}

export interface Transition {
  from: MissionState | null;
  to: MissionState;
  reason: string;
  at: string;
}

export interface Checkpoint {
  completed_task_ids: string[];
  note: string;
  at: string;
}

export type AgentRole =
  | "direct"
  | "coordinator"
  | "research"
  | "implementation"
  | "test"
  | "verification"
  | "security_review";

export interface PlanStep {
  step_id: string;
  description: string;
  capability: string | null;
  params: Record<string, unknown>;
  depends_on: string[];
  role: AgentRole;
  risk: string;
  estimated_cost_units: number;
}

export interface Plan {
  goal: string;
  source: string;
  rationale: string;
  steps: PlanStep[];
  waves: string[][];
  max_risk: string;
  estimated_cost_units: number;
  tool_calls: number;
  agent_calls: number;
  parallelisable: boolean;
}

export interface PlannedMission extends Plan {
  requires_approval: boolean;
  fits_budget: boolean;
  budget_problems: string[];
  notes: string[];
}

export interface Mission {
  mission_id: string;
  correlation_id: string;
  goal: string;
  state: MissionState;
  tasks: JarvisTask[];
  device_id: string | null;
  user_id: string;
  context: { plan?: PlannedMission } & Record<string, unknown>;
  history: Transition[];
  checkpoints: Checkpoint[];
  created_at: string;
  updated_at: string;
}

export interface MissionProgress {
  mission_id: string;
  goal: string;
  state: MissionState;
  tasks_total: number;
  tasks_done: number;
  tasks_failed: number;
  tasks_skipped: number;
  fraction_done: number;
  checkpoints: Checkpoint[];
  plan: PlannedMission | null;
  stopped_reason?: string;
}

export interface PendingApproval {
  fingerprint: string;
  capability: string;
  params: Record<string, unknown>;
  confirmation: string;
  mission_id: string | null;
  requested_at: string;
}

export interface CoreStatus {
  kill_switch: boolean;
  kill_switch_reason: string | null;
  events_published: number;
  active_missions: string[];
  pending_approvals: PendingApproval[];
  state: {
    devices: Array<{ device_id: string; online: boolean; trusted: boolean }>;
    active_missions: string[];
    presence: string | null;
  };
  world: Record<string, unknown>;
}

export type CapabilityHealth = "healthy" | "degraded" | "unavailable";

/** A registered, executable action (Capability Registry). */
export interface Capability {
  name: string;
  description: string;
  level: string;
  level_label: string;
  schema: Record<string, unknown>;
  required_grants: string[];
  health: CapabilityHealth;
  reversible: boolean;
  verifiable: boolean;
  undoable: boolean;
  tags: string[];
}

/** A scheduled job (Scheduler). Mirrors `ScheduledJob.to_dict()`. */
export interface ScheduledJobView {
  job_id: string;
  name: string;
  goal: string;
  kind: string;
  capability: string | null;
  params: Record<string, unknown>;
  grants: string[];
  device_id: string | null;
  next_run_at: string;
  interval_seconds: number | null;
  daily_at: string | null;
  /** For `kind: "after"`: the capability whose completion fires this job. */
  after_capability: string | null;
  enabled: boolean;
  attempts: number;
  max_attempts: number;
  backoff_base_seconds: number;
  last_run_at: string | null;
  last_outcome: string | null;
  last_detail: string | null;
  last_mission_id: string | null;
  runs: number;
  origin: string;
  created_at: string;
  needs_approval_each_run: boolean;
}

/** `Scheduler.snapshot()`. */
export interface SchedulerSnapshot {
  running: boolean;
  total: number;
  enabled: number;
  needing_approval: number;
  /** Enabled event-driven jobs waiting on a trigger, not on a clock. */
  armed: number;
  max_unattended_level: string;
  /** Earliest run among clock-scheduled jobs; null when only armed ones remain. */
  next_run_at: string | null;
  jobs: ScheduledJobView[];
}

/** One hash-chained entry from the Audit Logger. */
export interface AuditEntry {
  entry_id: string;
  timestamp: string;
  action: string;
  actor: string;
  subject: string;
  decision: string;
  correlation_id: string;
  prev_state: Record<string, unknown> | null;
  rollback_point: Record<string, unknown> | null;
  details: Record<string, unknown>;
  prev_hash: string;
  entry_hash: string;
}

export interface AuditSnapshot {
  chain_valid: boolean;
  first_broken_entry: string | null;
  entries: AuditEntry[];
}
