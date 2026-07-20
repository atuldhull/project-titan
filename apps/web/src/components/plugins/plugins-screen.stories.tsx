import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { PluginsScreen } from "./plugins-screen";

/**
 * §6.12 Plugin Manager, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 */
const meta: Meta<typeof PluginsScreen> = {
  title: "Screens/6.12 Plugin Manager",
  component: PluginsScreen,
};
export default meta;

type Story = StoryObj<typeof PluginsScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
