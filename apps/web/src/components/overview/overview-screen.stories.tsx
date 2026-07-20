import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { OverviewScreen } from "./overview-screen";

/**
 * §6.1 Organization Overview, in TITAN-FRT-002's four states.
 *
 * The real screen, mounted through the real hooks — only the network is faked
 * (`src/lib/stories/harness.tsx`). That distinction is the whole point of these
 * files: the pre-existing `Overview/KpiCard` and `Overview/ProjectCard` stories
 * already cover the pieces in every state, and covering the pieces is exactly
 * what let four screens ship without a branch that could reach one.
 */
const meta: Meta<typeof OverviewScreen> = {
  title: "Screens/6.1 Organization Overview",
  component: OverviewScreen,
};
export default meta;

type Story = StoryObj<typeof OverviewScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
