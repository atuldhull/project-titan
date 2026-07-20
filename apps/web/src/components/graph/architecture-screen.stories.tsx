import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { FIXTURE_IDS } from "@/lib/fixtures/api";

import { ArchitectureScreen } from "./architecture-screen";

/**
 * §6.8 Architecture Viewer, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 */
const meta: Meta<typeof ArchitectureScreen> = {
  title: "Screens/6.8 Architecture Viewer",
  component: ArchitectureScreen,
  args: { projectId: FIXTURE_IDS.project },
};
export default meta;

type Story = StoryObj<typeof ArchitectureScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
