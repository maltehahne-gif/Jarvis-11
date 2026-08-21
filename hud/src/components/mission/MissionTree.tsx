/**
 * Goal tree - Blueprint 3.2's Mission row: "goal tree, agents, dependencies".
 *
 * Positions come straight from `Plan.waves` - the Planner's own topological
 * levels (`planner/plan.py`'s `waves()`). A wave is a column; a step's
 * position within it is a row. Dependency lines are drawn from that same
 * data, so the picture is a direct rendering of what the Planner actually
 * computed, not a separate layout guess about it.
 *
 * Task state is joined in by `step_id` - `PlanStep.to_task()` reuses the
 * step id as the task id specifically so this join is possible.
 */

import type { AgentRole, JarvisTask, PlannedMission, TaskState } from "../../types/api";

const COL_WIDTH = 220;
const ROW_HEIGHT = 68;
const NODE_WIDTH = 180;
const NODE_HEIGHT = 46;

const ROLE_LABEL: Record<AgentRole, string> = {
  direct: "direct",
  coordinator: "coordinator",
  research: "research",
  implementation: "agent",
  test: "test",
  verification: "verify",
  security_review: "security",
};

const STATE_COLOR: Record<TaskState, string> = {
  PENDING: "var(--text-tertiary)",
  RUNNING: "var(--amber)",
  DONE: "var(--ok)",
  FAILED: "var(--danger)",
  SKIPPED: "var(--text-tertiary)",
};

interface Position {
  x: number;
  y: number;
}

export function MissionTree({ plan, tasks }: { plan: PlannedMission; tasks: JarvisTask[] }) {
  const taskById = new Map(tasks.map((t) => [t.task_id, t]));
  const positions = new Map<string, Position>();

  plan.waves.forEach((wave, col) => {
    wave.forEach((stepId, row) => {
      positions.set(stepId, { x: col * COL_WIDTH, y: row * ROW_HEIGHT });
    });
  });

  const width = plan.waves.length * COL_WIDTH;
  const maxRows = Math.max(1, ...plan.waves.map((w) => w.length));
  const height = maxRows * ROW_HEIGHT;

  const edges: { from: Position; to: Position; key: string }[] = [];
  for (const step of plan.steps) {
    const to = positions.get(step.step_id);
    if (!to) continue;
    for (const dep of step.depends_on) {
      const from = positions.get(dep);
      if (!from) continue;
      edges.push({ from, to, key: `${dep}->${step.step_id}` });
    }
  }

  return (
    <div className="mission-tree" style={{ width, height: height + NODE_HEIGHT }}>
      <svg className="mission-tree__edges" width={width + NODE_WIDTH} height={height + NODE_HEIGHT}>
        {edges.map((edge) => {
          const x1 = edge.from.x + NODE_WIDTH;
          const y1 = edge.from.y + NODE_HEIGHT / 2;
          const x2 = edge.to.x;
          const y2 = edge.to.y + NODE_HEIGHT / 2;
          const midX = (x1 + x2) / 2;
          return (
            <path
              key={edge.key}
              className="mission-tree__edge"
              d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
              fill="none"
            />
          );
        })}
      </svg>

      {plan.steps.map((step) => {
        const pos = positions.get(step.step_id);
        if (!pos) return null;
        const task = taskById.get(step.step_id);
        const state = task?.state ?? "PENDING";
        return (
          <div
            key={step.step_id}
            className="mission-tree__node"
            style={{ left: pos.x, top: pos.y, width: NODE_WIDTH, height: NODE_HEIGHT }}
            data-state={state}
          >
            <span className="mission-tree__dot" style={{ background: STATE_COLOR[state] }} />
            <div className="mission-tree__body">
              <div className="mission-tree__desc">{step.description}</div>
              <div className="mission-tree__meta mono">
                {ROLE_LABEL[step.role]}
                {step.capability ? ` · ${step.capability}` : ""}
                {step.risk !== "P0" && step.risk !== "P1" ? ` · ${step.risk}` : ""}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
