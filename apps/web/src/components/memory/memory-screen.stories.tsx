import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { MemoryScreen } from "./memory-screen";

/**
 * §6.11 Memory Explorer, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 */
const meta: Meta<typeof MemoryScreen> = {
  title: "Screens/6.11 Memory Explorer",
  component: MemoryScreen,
};
export default meta;

type Story = StoryObj<typeof MemoryScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
