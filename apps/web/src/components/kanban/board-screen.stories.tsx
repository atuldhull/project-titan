import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { FIXTURE_IDS } from "@/lib/fixtures/api";

import { BoardScreen } from "./board-screen";

/**
 * §6.3 Kanban Board, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * The existing `Kanban/Board` stories cover the presentational columns and
 * cards; these cover the screen that has to reach them.
 */
const meta: Meta<typeof BoardScreen> = {
  title: "Screens/6.3 Kanban Board",
  component: BoardScreen,
  args: { projectId: FIXTURE_IDS.project },
};
export default meta;

type Story = StoryObj<typeof BoardScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
