import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import type { components } from "@titan/schemas";

import { ProjectCard, ProjectCardSkeleton } from "./project-card";

type ProjectOut = components["schemas"]["ProjectOut"];
type Board = components["schemas"]["Board"];

const project: ProjectOut = {
  id: "p1",
  name: "netflix-clone",
  display_name: "netflix-clone",
  brief: "A streaming catalogue",
  status: "active",
  mode: "autonomous",
  llm_profile: "default",
  created_by: "u1",
  created_at: "2026-07-16T09:00:00Z",
  version: 1,
};

function board(counts: Record<string, number>): Board {
  return {
    project_id: "p1",
    columns: Object.entries(counts).map(([status, n]) => ({
      status,
      tasks: Array.from({ length: n }, (_, i) => ({
        id: `${status}-${i}`,
      })) as Board["columns"][number]["tasks"],
    })),
  };
}

/** Active-project card in all four states (TITAN-FRT-002). */
const meta: Meta<typeof ProjectCard> = {
  title: "Overview/ProjectCard",
  component: ProjectCard,
};
export default meta;

type Story = StoryObj<typeof ProjectCard>;

export const Populated: Story = {
  args: {
    project,
    board: board({ done: 7, merged: 3, in_progress: 2, ready: 2 }),
    sprint: {
      id: "s1",
      project_id: "p1",
      number: 3,
      status: "active",
      goal: null,
      starts_at: null,
      ends_at: null,
      created_at: "2026-07-16T09:00:00Z",
      version: 1,
    },
    agents: { total: 9, working: 4 },
  },
};

export const WithBlockedWork: Story = {
  args: {
    ...Populated.args,
    board: board({ done: 3, in_progress: 2, blocked: 4, escalated: 1 }),
  },
};

export const Loading: Story = {
  name: "Loading (skeleton shaped like the card)",
  render: () => <ProjectCardSkeleton />,
};

export const Empty: Story = {
  name: "Empty (no work yet → no fake 0%)",
  args: { project, board: board({}), agents: { total: 0, working: 0 } },
};

export const Error: Story = {
  name: "Error (board failed → progress unknown)",
  args: { project, board: undefined, agents: { total: 2, working: 0 } },
};
