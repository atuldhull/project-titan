import { useEffect } from "react";
import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import {
  useConnectionStore,
  type ConnectionStatus,
} from "@/lib/ws/connection-store";
import { ConnectionPill } from "./connection-pill";

/**
 * The always-visible realtime connection pill in each state (TITAN-FRT-020),
 * rendered in both themes via the toolbar. Each story drives the connection
 * store so the pill reflects live / reconnecting / offline / connecting.
 */
function PillWithStatus({ status }: { status: ConnectionStatus }) {
  useEffect(() => {
    useConnectionStore.setState({ status, degraded: status === "offline" });
  }, [status]);
  return <ConnectionPill />;
}

const meta: Meta<typeof PillWithStatus> = {
  title: "Shell/ConnectionPill",
  component: PillWithStatus,
};
export default meta;

type Story = StoryObj<typeof PillWithStatus>;

export const Live: Story = { args: { status: "live" } };
export const Connecting: Story = { args: { status: "connecting" } };
export const Reconnecting: Story = { args: { status: "reconnecting" } };
export const Offline: Story = { args: { status: "offline" } };
