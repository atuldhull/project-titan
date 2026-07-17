import type { Metadata } from "next";

import { ConversationsScreen } from "@/components/conversations/conversations-screen";

export const metadata: Metadata = { title: "Conversations" };

/**
 * Live agent conversations (Vol 4 §6.6).
 *
 * An optional catch-all: `/conversations` picks the first channel, and
 * `/conversations/{id}` opens one. The `id` is a PROJECT id — a project is the
 * only real channel this system has (see `lib/conversations/hooks`).
 */
export default async function Page({
  params,
}: {
  params: Promise<{ channelId?: string[] }>;
}) {
  const { channelId } = await params;
  return <ConversationsScreen channelId={channelId?.[0] ?? null} />;
}
