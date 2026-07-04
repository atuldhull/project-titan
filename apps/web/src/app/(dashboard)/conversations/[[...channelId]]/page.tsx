import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Conversations" };

export default async function Page({
  params,
}: {
  params: Promise<{ channelId?: string[] }>;
}) {
  const { channelId } = await params;
  const channel = channelId?.[0];
  return (
    <StubPage
      title="Live Agent Conversations"
      description="Channel list and live typed-message feed between agents (Vol 4 §6.6). A first live feed appears with the Phase-25 walking skeleton."
    >
      {channel ? (
        <p className="mt-3 font-mono text-xs text-muted-foreground">
          channel: {channel}
        </p>
      ) : null}
    </StubPage>
  );
}
