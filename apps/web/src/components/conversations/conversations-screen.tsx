"use client";

/**
 * Live Agent Conversations (Vol 4 §6.6, TITAN-FRT-028/029).
 *
 * §6.6 is the screen whose backend is furthest from its description, so this is
 * what it really is:
 *
 * - **A channel is a project.** No channel entity exists, and while agent
 *   threads are real (the bus inherits `correlation_id` into `thread_id`),
 *   nothing can ENUMERATE threads — so they cannot be listed as channels
 *   either. See `lib/conversations/hooks`. There is no org-wide message
 *   endpoint, so "org channels" have nothing to enumerate from.
 * - **No unread badges.** There is no per-message read state anywhere — the
 *   only `read_at` in the schema is on notifications. A badge would be invented.
 * - **No mute.** Zero hits for mute/muted across the backend.
 * - **No history search.** No message search endpoint; the schema's only
 *   full-text index is on `memories`.
 * - **No message.chunk.** The streaming bubble rides `run.output.delta`, whose
 *   only producer today is the CEO intake flow (`ceo_intake_enabled`, off by
 *   default). That is a real frame in the project's channel, so the code is
 *   real — it is just rarely exercised.
 *
 * What IS real: the message log, its kinds, cursor pagination on scroll-up,
 * live append via `message.created`, posting as a human, the type filter, and
 * task deep links.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { isDomainFrame } from "@titan/ws-protocol";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { MessageRow } from "@/components/conversations/message-row";
import { StreamBubble } from "@/components/conversations/stream-bubble";
import { Composer } from "@/components/conversations/composer";
import { ChannelList } from "@/components/conversations/channel-list";
import { useChannelMessages } from "@/lib/conversations/hooks";
import {
  filterByKind,
  isAtBottom,
  isBehindModal,
  liveStreams,
  orderOldestFirst,
  pruneStreams,
  reduceStreams,
  scrollTopAfterPrepend,
  type Streams,
} from "@/lib/conversations/feed";
import { MESSAGE_KINDS, kindMeta } from "@/lib/conversations/kinds";
import { useProjects } from "@/lib/projects/hooks";
import { createRafBatcher } from "@/lib/ws/raf-batch";
import { useWsClient } from "@/lib/ws/manager";
import { useTopic } from "@/lib/ws/use-topic";
import { useConnection } from "@/lib/ws/connection-store";
import { cn } from "@/lib/utils";

export function ConversationsScreen({
  channelId,
}: {
  channelId: string | null;
}) {
  const router = useRouter();
  const projects = useProjects();
  const channels = useMemo(() => projects.data?.items ?? [], [projects.data]);

  // Default to the first channel rather than showing an empty pane.
  const active = channelId ?? channels[0]?.id ?? null;

  const messages = useChannelMessages(active);
  const [kinds, setKinds] = useState<ReadonlySet<string>>(new Set());
  const [streams, setStreams] = useState<Streams>(new Map());
  const [pending, setPending] = useState(0);
  const [pinned, setPinned] = useState(true);

  const scroller = useRef<HTMLDivElement>(null);
  // Where the reader was, captured just before a history page is requested.
  const prependAnchor = useRef<{
    scrollTop: number;
    scrollHeight: number;
  } | null>(null);
  const pinnedRef = useRef(pinned);
  pinnedRef.current = pinned;
  // The frame listener is registered once per channel; it must read the CURRENT
  // filter, not the one that happened to be set when it was registered.
  const kindsRef = useRef(kinds);
  kindsRef.current = kinds;

  useTopic("channelMessages", active ?? undefined);
  useTopic("projectRuns", active ?? undefined);
  const client = useWsClient();
  const { status: connection } = useConnection();

  const all = useMemo(
    () => orderOldestFirst(messages.data?.pages ?? []),
    [messages.data],
  );
  const shown = useMemo(() => filterByKind(all, kinds), [all, kinds]);
  const live = useMemo(() => liveStreams(streams), [streams]);

  // Token streams, rAF-batched (TITAN-FRT-021): deltas can arrive hundreds a
  // second and one render each would blow the frame budget.
  useEffect(() => {
    if (!client || !active) {
      return;
    }
    const batcher = createRafBatcher<{
      runId: string;
      agentId: string | null;
      delta: string;
    }>((batch) => {
      setStreams((prev) =>
        batch.reduce(
          (acc, item) =>
            reduceStreams(acc, {
              type: "delta",
              runId: item.runId,
              agentId: item.agentId,
              delta: item.delta,
            }),
          prev,
        ),
      );
    });
    const unsubscribe = client.onAnyFrame((frame) => {
      if (!isDomainFrame(frame) || frame.project_id !== active) {
        return;
      }
      if (frame.type === "run.output.delta") {
        const payload = frame.payload as {
          run_id?: unknown;
          delta?: unknown;
          agent_id?: unknown;
        };
        if (
          typeof payload.delta === "string" &&
          typeof payload.run_id === "string"
        ) {
          batcher.push({
            runId: payload.run_id,
            agentId:
              typeof payload.agent_id === "string" ? payload.agent_id : null,
            delta: payload.delta,
          });
        }
      } else if (frame.type === "run.finished") {
        const runId = (frame.payload as { run_id?: unknown }).run_id;
        if (typeof runId === "string") {
          // Flush first: a finished frame can overtake the deltas still sitting
          // in this frame's batch, and marking done before they land would cut
          // the tail off the bubble.
          batcher.flush();
          setStreams((prev) =>
            reduceStreams(prev, { type: "finished", runId }),
          );
        }
      } else if (frame.type === "message.created") {
        // The query invalidation is the WS layer's job; this only counts what
        // arrived while the reader was scrolled away — and only what they would
        // actually SEE. The kind filter is client-side, so counting a hidden
        // kind promises "↓ 30 new" that clicking reveals nothing of.
        const kind = (frame.payload as { kind?: unknown }).kind;
        const visible =
          kindsRef.current.size === 0 ||
          (typeof kind === "string" && kindsRef.current.has(kind));
        if (!pinnedRef.current && visible) {
          setPending((n) => n + 1);
        }
      }
    });
    return () => {
      unsubscribe();
      batcher.dispose();
    };
  }, [client, active]);

  // Switching channel resets what is only true of the old one.
  useEffect(() => {
    setStreams(new Map());
    setPending(0);
    setPinned(true);
  }, [active]);

  const jumpToBottom = useCallback(() => {
    const el = scroller.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
    setPending(0);
    setPinned(true);
  }, []);

  // Auto-scroll ONLY when the reader is at the bottom (§6.6) — and hold their
  // place when history is prepended above them.
  //
  // Keyed on `shown.length` and the streamed TEXT LENGTH rather than on `live`
  // itself: `liveStreams` returns a fresh array on every render, so depending on
  // it would run this effect every render. The total length changes exactly when
  // a token actually lands, which is when the feed really grows.
  const streamedChars = live.reduce((n, s) => n + s.text.length, 0);
  useEffect(() => {
    const el = scroller.current;
    if (!el) {
      return;
    }
    if (pinnedRef.current) {
      // Pinned wins — but DROP the anchor rather than leaving it armed. A reader
      // who asked for history and then flicked back to the bottom before it
      // landed would otherwise leave a stale anchor primed; the next time they
      // scrolled up, the first frame to arrive would fire it and yank them.
      prependAnchor.current = null;
      el.scrollTop = el.scrollHeight;
      return;
    }
    const anchor = prependAnchor.current;
    if (anchor !== null) {
      // Older messages just landed ABOVE the viewport. Restoring the raw
      // scrollTop would leave the reader looking at a different message —
      // usually back at the top, which instantly asks for another page.
      el.scrollTop = scrollTopAfterPrepend(anchor, el.scrollHeight);
      prependAnchor.current = null;
    }
  }, [shown.length, streamedChars]);

  // A history fetch that FAILED (or returned nothing new) never grows the feed,
  // so the effect above never fires and never clears the anchor. Retire it as
  // soon as the fetch is no longer in flight.
  useEffect(() => {
    if (!messages.isFetchingNextPage) {
      prependAnchor.current = null;
    }
  }, [messages.isFetchingNextPage, messages.data]);

  // A finished stream's reply lands as a real `message.created` row, so once the
  // log grows the bubble is a duplicate. Retiring them here is also what keeps
  // the map from growing for the life of the tab.
  useEffect(() => {
    setStreams((prev) => pruneStreams(prev));
  }, [all.length]);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) {
      return;
    }
    const atBottom = isAtBottom(el);
    setPinned(atBottom);
    if (atBottom) {
      setPending(0);
    }
    // Scroll-up history (§6.6, cursor-based). Record where we are BEFORE the
    // page lands, so the effect above can put the reader back where they were.
    if (
      el.scrollTop < 64 &&
      messages.hasNextPage &&
      !messages.isFetchingNextPage
    ) {
      prependAnchor.current = {
        scrollTop: el.scrollTop,
        scrollHeight: el.scrollHeight,
      };
      void messages.fetchNextPage();
    }
  }, [messages]);

  // §10: Alt+↑/↓ switches channel. Scoped to this screen and skipped inside a
  // text input, per §10's own rule.
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (
        !event.altKey ||
        (event.key !== "ArrowUp" && event.key !== "ArrowDown")
      ) {
        return;
      }
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA)$/.test(target.tagName)) {
        return;
      }
      // Never navigate the page out from under a modal. A window listener sees
      // keys aimed at an open dropdown, dialog or sheet — Phase 60 shipped
      // exactly this bug with a Tab handler — and Alt+Down inside the topbar
      // menu silently switched channel behind it.
      if (isBehindModal(scroller.current)) {
        return;
      }
      const index = channels.findIndex((c) => c.id === active);
      if (index === -1) {
        return;
      }
      const next = event.key === "ArrowUp" ? index - 1 : index + 1;
      const target_ = channels[next];
      if (target_) {
        event.preventDefault();
        router.push(`/conversations/${target_.id}`);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [channels, active, router]);

  const toggleKind = (kind: string): void => {
    setKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) {
        next.delete(kind);
      } else {
        next.add(kind);
      }
      return next;
    });
  };

  if (projects.isLoading) {
    return <Skeleton className="h-96 rounded-lg" />;
  }
  if (projects.isError) {
    return (
      <p className="rounded-lg border bg-card p-6 text-sm text-destructive">
        Couldn’t load channels.
      </p>
    );
  }
  if (channels.length === 0) {
    return (
      <p className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
        No channels yet — a project’s conversation appears here once it exists.
      </p>
    );
  }

  return (
    <div className="grid gap-3 lg:grid-cols-[220px_1fr]">
      <ChannelList channels={channels} activeId={active} />

      <section className="flex h-[calc(100vh-10rem)] flex-col rounded-lg border bg-card">
        <header className="flex flex-wrap items-center gap-2 border-b p-2">
          <h2 className="text-sm font-medium">
            {channels.find((c) => c.id === active)?.display_name ?? "Channel"}
          </h2>
          {connection === "live" ? (
            <Badge variant="secondary">live</Badge>
          ) : (
            <Badge variant="outline">reconnecting</Badge>
          )}
          {/* The pressed state cannot ride on the badge VARIANT alone: `review`
              and `architecture` are themselves variant "outline", so toggling
              them changed nothing on screen — the filter worked and looked
              broken. A ring plus a ✓ marks it, which also satisfies
              TITAN-FRT-044 (status is never colour-only). */}
          <div className="ml-auto flex flex-wrap gap-1">
            {MESSAGE_KINDS.map((kind) => {
              const on = kinds.has(kind);
              return (
                <button
                  key={kind}
                  type="button"
                  onClick={() => toggleKind(kind)}
                  aria-pressed={on}
                  className={cn(
                    "rounded-md focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                    on &&
                      "ring-2 ring-primary ring-offset-1 ring-offset-background",
                  )}
                >
                  <Badge variant={on ? kindMeta(kind).variant : "outline"}>
                    {on ? <span aria-hidden>✓ </span> : null}
                    {kindMeta(kind).label}
                  </Badge>
                </button>
              );
            })}
          </div>
        </header>

        <div
          ref={scroller}
          onScroll={onScroll}
          className="relative flex-1 overflow-y-auto p-2"
        >
          {messages.isFetchingNextPage ? (
            <p className="py-2 text-center text-xs text-muted-foreground">
              Loading history…
            </p>
          ) : null}
          {connection !== "live" && all.length > 0 ? (
            <div className="my-2 flex items-center gap-2 text-xs text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              stream interrupted — reconnecting
              <span className="h-px flex-1 bg-border" />
            </div>
          ) : null}

          {messages.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }, (_, i) => (
                <Skeleton key={i} className="h-12 rounded-md" />
              ))}
            </div>
          ) : messages.isError ? (
            <p className="p-4 text-sm text-destructive">
              Couldn’t load this conversation.
            </p>
          ) : shown.length === 0 && live.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">
              {all.length === 0
                ? "No messages yet."
                : "No messages match this filter."}
            </p>
          ) : (
            // Streams render alongside the messages, NOT nested in the
            // non-empty branch. Putting them there hid them in the one
            // situation their only producer creates: the CEO intake flow
            // streams into a brand-new project, whose message log is empty —
            // so `shown.length === 0` and the live reply was discarded.
            <div className="divide-y">
              {shown.map((message) => (
                <MessageRow
                  key={message.id}
                  message={message}
                  projectId={active as string}
                />
              ))}
              {live.map((stream) => (
                <StreamBubble key={stream.runId} stream={stream} />
              ))}
            </div>
          )}
        </div>

        {pending > 0 ? (
          <div className="flex justify-center pb-1">
            <Button size="sm" variant="secondary" onClick={jumpToBottom}>
              ↓ {pending} new
            </Button>
          </div>
        ) : null}

        <Composer projectId={active} onSent={jumpToBottom} />
      </section>
    </div>
  );
}
