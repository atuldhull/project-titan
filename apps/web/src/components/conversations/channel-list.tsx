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
  className,
  onNavigate,
}: {
  channels: readonly ProjectOut[];
  activeId: string | null;
  /**
   * Visibility and sizing come from the screen, which is the only place that
   * knows whether this list is the ONE panel on show (§8, under 768) or one of
   * two (≥ 1024). Passing classes down beats a `variant` prop: there are three
   * bands and the list itself has no opinion about any of them.
   */
  className?: string;
  /** Closes the ≥768 Sheet this list is rendered inside; absent when inline. */
  onNavigate?: () => void;
}) {
  return (
    <nav
      aria-label="Channels"
      className={cn("rounded-lg border bg-card p-1", className)}
    >
      <ul className="space-y-0.5">
        {channels.map((channel) => (
          <li key={channel.id}>
            <Link
              href={`/conversations/${channel.id}`}
              aria-current={channel.id === activeId ? "page" : undefined}
              onClick={onNavigate}
              className={cn(
                // §8: touch targets ≥ 44px under `md`. This is a raw <a>, so it
                // inherits none of the primitives' `TOUCH_TARGET`; `max-md:`
                // scopes it so the ≥md list stays dense — §6.6 readers watch a
                // lot of rows at once.
                "flex items-center gap-1 rounded-md px-2 py-1.5 text-sm max-md:min-h-11",
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
      {/* A keyboard hint is noise on a touch device, where there is no Alt key
          and the space is scarce (§8) — and it would be a LIE in the Sheet copy
          of this list (`onNavigate` is what marks that copy). The screen's
          Alt+↑/↓ binding refuses to fire while an overlay covers the screen, so
          advertising it inside the overlay would promise a dead key. */}
      {onNavigate ? null : (
        <p className="hidden px-2 py-1.5 text-xs text-muted-foreground md:block">
          Alt+↑/↓ to switch
        </p>
      )}
    </nav>
  );
}
