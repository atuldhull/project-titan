import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ConversationsScreen } from "./conversations-screen";

/**
 * §6.6 Live Agent Conversations, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * `channelId: null` is a real state, not a placeholder: the route is
 * `/conversations/[[...channelId]]` and no channel selected is how it opens.
 */
const meta: Meta<typeof ConversationsScreen> = {
  title: "Screens/6.6 Live Agent Conversations",
  component: ConversationsScreen,
  args: { channelId: null },
};
export default meta;

type Story = StoryObj<typeof ConversationsScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
