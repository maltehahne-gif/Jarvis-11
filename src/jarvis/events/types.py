"""Canonical event type names.

Kept in one place so producers and consumers cannot drift apart on a typo.
The naming scheme is `<domain>.<subject>.<past-tense-verb>`, matching the
blueprint's own example `mission.task.started` (Blueprint 5.2).
"""

from __future__ import annotations

# --- command intake ---------------------------------------------------------
COMMAND_RECEIVED = "command.received"
COMMAND_ROUTED = "command.routed"
COMMAND_REJECTED = "command.rejected"

# --- mission lifecycle (Blueprint 5.3) --------------------------------------
MISSION_CREATED = "mission.created"
MISSION_STATE_CHANGED = "mission.state.changed"
MISSION_TASK_STARTED = "mission.task.started"
MISSION_TASK_FINISHED = "mission.task.finished"
MISSION_PROGRESS = "mission.progress"
MISSION_CHECKPOINT = "mission.checkpoint"
MISSION_APPROVAL_REQUESTED = "mission.approval.requested"
MISSION_APPROVAL_GRANTED = "mission.approval.granted"
MISSION_APPROVAL_DENIED = "mission.approval.denied"

# --- planning / agents ------------------------------------------------------
PLAN_CREATED = "plan.created"
AGENT_INVOKED = "agent.invoked"
AGENT_COMPLETED = "agent.completed"
AGENT_FAILED = "agent.failed"

# --- permission + execution -------------------------------------------------
PERMISSION_CHECKED = "permission.checked"
PERMISSION_DENIED = "permission.denied"
PERMISSION_CONFIRMATION_REQUIRED = "permission.confirmation.required"
TOOL_INVOKED = "tool.invoked"
TOOL_SUCCEEDED = "tool.succeeded"
TOOL_FAILED = "tool.failed"

# --- verification (Blueprint 5.4: "Tool aufgerufen" != "Ziel erreicht") ------
VERIFICATION_STARTED = "verification.started"
VERIFICATION_PASSED = "verification.passed"
VERIFICATION_FAILED = "verification.failed"

# --- memory (Blueprint 8) ---------------------------------------------------
MEMORY_STORED = "memory.stored"
MEMORY_DELETED = "memory.deleted"
MEMORY_ROUTINE_PROPOSED = "memory.routine.proposed"

# --- scheduler (Blueprint 5.1) ----------------------------------------------
JOB_REGISTERED = "scheduler.job.registered"
JOB_FIRED = "scheduler.job.fired"
JOB_SUCCEEDED = "scheduler.job.succeeded"
JOB_FAILED = "scheduler.job.failed"
JOB_RETRY_SCHEDULED = "scheduler.job.retry_scheduled"
JOB_PARKED = "scheduler.job.parked"
JOB_EXHAUSTED = "scheduler.job.exhausted"

# --- safety -----------------------------------------------------------------
WATCHDOG_TRIPPED = "safety.watchdog.tripped"
KILL_SWITCH_ENGAGED = "safety.kill_switch.engaged"
KILL_SWITCH_RELEASED = "safety.kill_switch.released"
BUDGET_EXCEEDED = "safety.budget.exceeded"
CAPABILITY_GRANT_EXPIRED = "safety.capability_grant.expired"
