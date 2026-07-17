"use client";

import Link from "next/link";

import type { ProjectOut } from "@titan/api-client";

import { cn } from "@/lib/utils";

/**
 * The channel list.
 *
 * One channel per project, because a project is the only real channel this
 * system has — see `lib/conversations/hooks` for why threads are not one and
 * why there are no org channels.
 *
 * §6.6 shows unread badges. There is no per-message read state anywhere in the
 * schema (the only `read_at` is on notifications), so there is nothing to count
 * and no badge is rendered. A badge fed by a guess would be worse than none.
 */
export function ChannelList({
  channels,
  activeId,
}: {
  channels: readonly ProjectOut[];
  activeId: string | null;
}) {
  return (
    <nav aria-label="Channels" className="rounded-lg border bg-card p-1">
      <ul className="space-y-0.5">
        {channels.map((channel) => (
          <li key={channel.id}>
            <Link
              href={`/conversations/${channel.id}`}
              aria-current={channel.id === activeId ? "page" : undefined}
              className={cn(
                "flex items-center gap-1 rounded-md px-2 py-1.5 text-sm",
                "focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                channel.id === activeId
                  ? "bg-accent font-medium text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50",
              )}
            >
              <span aria-hidden className="text-muted-foreground">
                #
              </span>
              <span className="truncate">{channel.name}</span>
            </Link>
          </li>
        ))}
      </ul>
      <p className="px-2 py-1.5 text-xs text-muted-foreground">
        Alt+↑/↓ to switch
      </p>
    </nav>
  );
}
