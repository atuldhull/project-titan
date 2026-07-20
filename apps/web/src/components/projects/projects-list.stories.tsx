import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ProjectsList } from "./projects-list";

/**
 * §6.2 Projects, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * §6.2 is ONE screen specification covering the projects LIST and a project's
 * DETAIL. The list is the route the spec names first and the one the sweep
 * treats as §6.2's own.
 */
const meta: Meta<typeof ProjectsList> = {
  title: "Screens/6.2 Projects",
  component: ProjectsList,
};
export default meta;

type Story = StoryObj<typeof ProjectsList>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
