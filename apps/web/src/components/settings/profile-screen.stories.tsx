import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ProfileScreen } from "./profile-screen";

/**
 * §6.13 Settings, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * §6.13 is one screen specification over seven sub-pages. `/settings`
 * redirects to Profile, so Profile is what this registry and the sweep treat
 * as §6.13's own; the other six ride along in the sweep's `alsoVisit` list.
 */
const meta: Meta<typeof ProfileScreen> = {
  title: "Screens/6.13 Settings",
  component: ProfileScreen,
};
export default meta;

type Story = StoryObj<typeof ProfileScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
