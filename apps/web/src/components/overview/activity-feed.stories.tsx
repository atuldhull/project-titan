import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ActivityFeed, type ActivityRow } from "./activity-feed";

/**
 * The live-activity feed (§6.1). Stories inject rows so the feed renders without
 * a socket. Note the honest scope: rows show what the wire actually carries
 * (frame type + time), because `org:activity` has no actor/verb/entity and no
 * history endpoint exists to backfill from.
 */
const meta: Meta<typeof ActivityFeed> = {
  title: "Overview/ActivityFeed",
  component: ActivityFeed,
};
export default meta;

type Story = StoryObj<typeof ActivityFeed>;

const rows: ActivityRow[] = [
  {
    id: "1",
    ts: "2026-07-16T12:04:00Z",
    type: "task.created",
    projectId: "p1",
  },
  {
    id: "2",
    ts: "2026-07-16T12:04:00Z",
    type: "run.finished",
    projectId: "p1",
  },
  {
    id: "3",
    ts: "2026-07-16T12:03:00Z",
    type: "decision.recorded",
    projectId: "p2",
  },
  {
    id: "4",
    ts: "2026-07-16T12:01:00Z",
    type: "pipeline.updated",
    projectId: "p1",
  },
  {
    id: "5",
    ts: "2026-07-16T11:58:00Z",
    type: "agent.status",
    projectId: null,
  },
];

export const Populated: Story = { args: { rows } };

export const Empty: Story = {
  name: "Empty (live-only: nothing to backfill)",
  args: { rows: [] },
};

export const Busy: Story = {
  name: "Populated (virtualized, 200 rows)",
  args: {
    rows: Array.from({ length: 200 }, (_, i) => ({
      id: String(i),
      ts: "2026-07-16T12:00:00Z",
      type: i % 3 === 0 ? "task.updated" : "run.started",
      projectId: "p1",
    })),
  },
};
