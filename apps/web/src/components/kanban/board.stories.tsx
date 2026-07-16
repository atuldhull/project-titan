import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { DndContext } from "@dnd-kit/core";

import type { components } from "@titan/schemas";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Card } from "@/lib/kanban/board-store";

import {
  BoardColumn,
  BoardEmpty,
  BoardError,
  BoardSkeleton,
} from "./board-column";
import { TaskCard } from "./task-card";

type AgentOut = components["schemas"]["AgentOut"];

/**
 * The Kanban board's four states (§6.3).
 *
 * The states are storied through the presentational pieces rather than
 * `BoardScreen`, which fetches its own data (board + workflow + graph + agents)
 * and holds a socket. Note what is NOT here, and is not an oversight: no WIP
 * limits (nothing defines one), no labels (no such column exists), and the ids
 * are UUID prefixes because tasks have no "T-231" key. Each is documented at its
 * component.
 */
const meta: Meta = { title: "Kanban/Board" };
export default meta;

const AGENT: AgentOut = {
  id: "a1",
  project_id: "p1",
  role: "backend",
  name: "backend-bot",
  status: "busy",
  model_binding: { provider: "ollama", model: "qwen2.5-coder:7b" },
  config: {},
  current_task_id: null,
  created_at: "2026-07-16T00:00:00Z",
  version: 1,
};

function card(id: string, title: string, over: Partial<Card> = {}): Card {
  return {
    id,
    title,
    status: "in_progress",
    priority: 3,
    estimate_points: 3,
    assignee_agent_id: "a1",
    sprint_id: null,
    ...over,
  };
}

/** Loading: 5 skeleton columns x 3 skeleton cards. */
export const Loading: StoryObj = { render: () => <BoardSkeleton /> };

/** Empty: planning has not produced tasks yet. */
export const Empty: StoryObj = { render: () => <BoardEmpty /> };

/** Error: a board-level retry panel. */
export const Error: StoryObj = {
  render: () => <BoardError onRetry={() => undefined} />,
};

/** Ready: real columns, with the card variants that actually occur. */
export const Ready: StoryObj = {
  render: () => (
    <TooltipProvider>
      <DndContext>
        <div className="flex gap-3">
          <BoardColumn
            status="backlog"
            count={2}
            isDropTarget={false}
            isRejecting={false}
            stale={false}
          >
            <TaskCard
              card={card("0192f4a1-1111-7000-8000-000000000001", "Auth API", {
                status: "backlog",
                assignee_agent_id: null,
                estimate_points: null,
              })}
              agent={undefined}
              unmetDeps={0}
              draggable
              pulsing={false}
              animating={false}
              onOpen={() => undefined}
            />
            <TaskCard
              card={card("0192f4a1-2222-7000-8000-000000000002", "Payments", {
                status: "backlog",
              })}
              agent={AGENT}
              unmetDeps={2}
              draggable
              pulsing={false}
              animating={false}
              onOpen={() => undefined}
            />
          </BoardColumn>

          <BoardColumn
            status="in_progress"
            count={1}
            isDropTarget
            isRejecting={false}
            stale={false}
          >
            <TaskCard
              card={card("0192f4a1-3333-7000-8000-000000000003", "Schema DB")}
              agent={AGENT}
              unmetDeps={0}
              draggable
              pulsing
              animating
              onOpen={() => undefined}
            />
          </BoardColumn>

          {/* The socket is down: every column is equally stale, because the
              stream is per project — there is no per-column feed to fail. */}
          <BoardColumn
            status="done"
            count={0}
            isDropTarget={false}
            isRejecting={false}
            stale
          >
            {null}
          </BoardColumn>
        </div>
      </DndContext>
    </TooltipProvider>
  ),
};

/** A viewer: no drag handles at all — `task:transition` gates every move. */
export const ReadOnly: StoryObj = {
  render: () => (
    <TooltipProvider>
      <DndContext>
        <BoardColumn
          status="backlog"
          count={1}
          isDropTarget={false}
          isRejecting={false}
          stale={false}
        >
          <TaskCard
            card={card("0192f4a1-4444-7000-8000-000000000004", "Auth API", {
              status: "backlog",
            })}
            agent={AGENT}
            unmetDeps={0}
            draggable={false}
            pulsing={false}
            animating={false}
            onOpen={() => undefined}
          />
        </BoardColumn>
      </DndContext>
    </TooltipProvider>
  ),
};
