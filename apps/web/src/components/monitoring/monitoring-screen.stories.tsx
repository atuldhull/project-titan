import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { MonitoringScreen } from "./monitoring-screen";

/**
 * §6.10 Monitoring, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 */
const meta: Meta<typeof MonitoringScreen> = {
  title: "Screens/6.10 Monitoring",
  component: MonitoringScreen,
};
export default meta;

type Story = StoryObj<typeof MonitoringScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
