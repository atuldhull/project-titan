import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { OrgDeployments } from "./org-deployments";

/**
 * §6.9 Deployment Dashboard, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * The org-wide route. Its Error story is the one that would have caught this
 * screen shipping with no `isError` branch at all, which rendered a failed
 * project fetch as "No projects yet."
 */
const meta: Meta<typeof OrgDeployments> = {
  title: "Screens/6.9 Deployment Dashboard",
  component: OrgDeployments,
};
export default meta;

type Story = StoryObj<typeof OrgDeployments>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
