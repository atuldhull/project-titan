"use client";

import { Badge } from "@/components/ui/badge";
import type { Stream } from "@/lib/conversations/feed";
import { initials } from "@/lib/agents/initials";

/**
 * A bubble growing from `run.output.delta` tokens (§6.6's "▍streaming reply…").
 *
 * Attributed to a RUN and its agent, not to a message — that is the honest
 * limit of the only token frame that exists (see `lib/conversations/feed`).
 *
 * The caret blinks only under `motion-safe` (TITAN-FRT-045 names "streaming
 * caret blink" outright). Under `prefers-reduced-motion` it stays a solid
 * block: the requirement is that the ANIMATION stops, not the data — the text
 * keeps streaming either way.
 */
export function StreamBubble({ stream }: { stream: Stream }) {
  const who = stream.agentId ? stream.agentId.slice(0, 8) : "agent";
  return (
    // NOT a live region. React replaces the whole text node on every rAF batch,
    // so `aria-live` made a screen reader restart the entire accumulated reply
    // several times a second and never finish one — a 2,000-token stream would
    // flood the polite queue and drown every other announcement on the page.
    // The finished message arrives as an ordinary `message.created` row, which
    // is the thing worth reading; this bubble is a progress indicator, and the
    // status line below announces it once.
    <article className="flex gap-2 px-1 py-2">
      <span
        aria-hidden
        className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-medium"
      >
        {initials(who)}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-baseline gap-1.5">
          <span className="text-xs font-medium">{who}</span>
          <Badge variant="secondary">streaming</Badge>
        </div>
        <p className="text-sm break-words whitespace-pre-wrap">
          {stream.text}
          <span
            aria-hidden
            className="ml-0.5 inline-block motion-safe:animate-pulse"
          >
            ▍
          </span>
        </p>
      </div>
    </article>
  );
}
