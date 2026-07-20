import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { LogsScreen } from "./logs-screen";

/**
 * §6.7 Logs Viewer, in TITAN-FRT-002's four states.
 *
 * The real screen through the real hooks — only the network is stubbed
 * (`src/lib/stories/harness.tsx`).
 *
 * **Why this is not in `logs-screen.stories.tsx`.** That file already exists and
 * holds the `Logs/LogsScreen` COMPONENT stories, which render extracted rows,
 * gap markers and the filter bar through a harness defined inside it — and
 * deliberately do not mount `LogsScreen`, because the screen owns a virtualizer
 * that has no height in a story frame. Both files are wanted: those cover the
 * pieces in states this one cannot reach (a stream gap, an applied filter), and
 * this one covers the four fetch states of the screen itself.
 */
const meta: Meta<typeof LogsScreen> = {
  title: "Screens/6.7 Logs Viewer",
  component: LogsScreen,
};
export default meta;

type Story = StoryObj<typeof LogsScreen>;

export const Loading: Story = { parameters: { screenState: "loading" } };
export const Empty: Story = { parameters: { screenState: "empty" } };
export const Error: Story = { parameters: { screenState: "error" } };
export const Populated: Story = { parameters: { screenState: "populated" } };
