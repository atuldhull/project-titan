import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { FIXTURE_IDS } from "@/lib/fixtures/api";

import { TaskGraphScreen } from "./task-graph-screen";

/**
 * §6.4 Task Graph, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * The canvas sits behind `next/dynamic`, so a populated story shows the
 * shell's skeleton until React Flow resolves — the screen's real loading
 * behaviour, not a story artifact.
 */
const meta: Meta<typeof TaskGraphScreen> = {
  title: "Screens/6.4 Task Graph",
  component: TaskGraphScreen,
  args: { projectId: FIXTURE_IDS.project },
};
export default meta;

type Story = StoryObj<typeof TaskGraphScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
