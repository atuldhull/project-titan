import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { AgentGrid } from "./agent-grid";

/**
 * §6.5 Agent Status Grid, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 */
const meta: Meta<typeof AgentGrid> = {
  title: "Screens/6.5 Agent Status Grid",
  component: AgentGrid,
};
export default meta;

type Story = StoryObj<typeof AgentGrid>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
