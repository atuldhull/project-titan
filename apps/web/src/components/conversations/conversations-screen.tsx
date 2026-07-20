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
 *
 * The responsive rules (§8) and the live region (§9) are commented at the
 * markup they govern; the one thing worth stating up here is that the screen no
 * longer sizes itself from the viewport. It was `h-[calc(100vh-10rem)]`, which
 * was wrong three ways at once: `100vh` ignores mobile browser chrome (the
 * shell uses `h-dvh` for exactly that reason), the `10rem` was a guess at the
 * topbar + padding that no longer matched either, and it budgeted nothing for
 * the `< 768` bottom tab bar — so the composer sat under it. `h-full` against
 * the shell's already-correct `<main>` box, plus `min-h-0` so the scroller may
 * shrink below its content, needs no arithmetic and cannot drift.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronLeft, Hash } from "lucide-react";

import { isDomainFrame } from "@titan/ws-protocol";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  MessageRow,
  senderLabel,
} from "@/components/conversations/message-row";
import { StreamBubble } from "@/components/conversations/stream-bubble";
import { Composer } from "@/components/conversations/composer";
import { ChannelList } from "@/components/conversations/channel-list";
import { useChannelMessages } from "@/lib/conversations/hooks";
import {
  announceArrivals,
  filterByKind,
  isAtBottom,
  liveStreams,
  orderOldestFirst,
  pruneStreams,
  reduceStreams,
  scrollTopAfterPrepend,
  type Streams,
} from "@/lib/conversations/feed";
import { MESSAGE_KINDS, kindMeta } from "@/lib/conversations/kinds";
import { summarize } from "@/lib/conversations/refs";
import { useProjects } from "@/lib/projects/hooks";
import { isBehindOverlay } from "@/lib/shortcuts/keys";
import { useShortcuts } from "@/lib/shortcuts/provider";
import { createRafBatcher } from "@/lib/ws/raf-batch";
import { useWsClient } from "@/lib/ws/manager";
import { useTopic } from "@/lib/ws/use-topic";
import { useConnection } from "@/lib/ws/connection-store";
import { cn } from "@/lib/utils";

/**
 * Flipped into the live region so a repeated sentence still counts as a change.
 * Not voiced by any screen reader; see the region's own comment.
 */
const ZERO_WIDTH_SPACE = "\u200B";

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
  // §8, 768–1023: the feed goes full-bleed and the channel list becomes
  // reachable rather than resident. Below 768 this Sheet is never opened — the
  // URL swaps the panels outright — and at ≥1024 its trigger is not rendered.
  const [channelSheet, setChannelSheet] = useState(false);

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

  // --- the polite live region (§9 / TITAN-FRT-043) --------------------------
  //
  // A SEPARATE sr-only region, not `aria-live` on the scroller. The scroller is
  // the thing history is prepended into and the thing the kind filter rebuilds,
  // so a live region on it would read a screenful of OLD messages aloud every
  // time the reader paged up or toggled a chip. `announceArrivals` decides what
  // is genuinely new; this only holds the string.
  //
  // `announcement` carries a PARITY bit as well as its text. A live region only
  // speaks when its content CHANGES, and two consecutive arrivals routinely
  // produce byte-identical text: `summarize` returns null for any body without
  // a summary/text/title field — a `progress_report` of `{percent: 40}` is one —
  // so two progress reports in a row from the same agent both render
  // "New message: PM-1, Progress" and the second is silently never spoken.
  // Flipping a zero-width space (which no screen reader voices) guarantees the
  // node's text differs from the previous one whatever the sentence says.
  const [announcement, setAnnouncement] = useState({ text: "", parity: false });
  const spokenId = useRef<string | null>(null);

  // Re-seed on channel switch: the first render of a new channel's log is a
  // navigation result, not an arrival, and announcing its newest message would
  // duplicate what the reader just chose.
  //
  // Declared BEFORE the announcing effect, and that order is load-bearing.
  // Effects run in declaration order within a commit, and a channel whose
  // messages are already in the query cache changes `active` and `shown` in the
  // SAME commit. Re-seeding second would null the anchor the announcer had just
  // set, so the next genuine arrival would be swallowed as a seed and the
  // reader's first message on that channel would never be spoken.
  useEffect(() => {
    spokenId.current = null;
    setAnnouncement({ text: "", parity: false });
  }, [active]);

  useEffect(() => {
    const next = announceArrivals(shown, spokenId.current, (message) => {
      const summary = summarize(message);
      const label = `${senderLabel(message)}, ${kindMeta(message.kind).label}`;
      return summary ? `${label}. ${summary}` : label;
    });
    if (!next) {
      return;
    }
    spokenId.current = next.lastId;
    if (next.text !== "") {
      setAnnouncement((prev) => ({ text: next.text, parity: !prev.parity }));
    }
  }, [shown]);

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

  // §10: Alt+↑/↓ switches channel.
  //
  // Registered with the global engine rather than on `window`. The hand-rolled
  // listener this replaces guarded with `/^(INPUT|TEXTAREA)$/` on the event
  // target, which misses `contenteditable` and Radix's `role="textbox"` and
  // `role="combobox"` triggers — the engine's `isTextEntry` walks ancestors and
  // covers all of them, and it owns `preventDefault` too, so neither is repeated
  // here. Navigating to `/conversations/{id}` is also what makes the ≥768
  // one-panel rule below follow along: the URL is the panel selector.
  //
  // What the engine does NOT replace is the old `isBehindModal` call. Its
  // `isBehindOverlay` asks about the EVENT TARGET — "is what the user is aiming
  // at hidden?" — and inside an open dialog the answer is no: Radix hides the
  // rest of the page and puts focus in the dialog. The question a SCREEN-scoped
  // navigation has to ask is the other one, "is my screen the one the user can
  // see?", and only the screen can ask it, about its own element. Without this,
  // Alt+↓ from inside the `?` help overlay silently switches channel behind it —
  // which is the Phase 60 bug the original guard was written for.
  const switchChannel = useCallback(
    (delta: number): void => {
      if (isBehindOverlay(scroller.current)) {
        return;
      }
      const index = channels.findIndex((c) => c.id === active);
      if (index === -1) {
        return;
      }
      const next = channels[index + delta];
      if (next) {
        router.push(`/conversations/${next.id}`);
      }
    },
    [channels, active, router],
  );

  useShortcuts([
    {
      id: "conversations-prev",
      keys: "Alt+ArrowUp",
      label: "Previous channel",
      group: "Conversations",
      run: () => switchChannel(-1),
    },
    {
      id: "conversations-next",
      keys: "Alt+ArrowDown",
      label: "Next channel",
      group: "Conversations",
      run: () => switchChannel(1),
    },
  ]);

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

  const channelName =
    channels.find((c) => c.id === active)?.display_name ?? "Channel";

  return (
    /*
     * §8's three bands, all of them pure CSS — there is no `matchMedia` in this
     * app and adding one would need a jsdom mock the suite does not have.
     *
     *   < 768   ONE panel. Which one is derived from `channelId`, i.e. from the
     *           URL, so the choice survives a reload and a share (TITAN-FRT-006)
     *           and the back button unwinds it. Note this reads `channelId`, NOT
     *           `active`: `active` falls back to the first channel so the ≥768
     *           layout always has a feed to show, and using it here would mean
     *           `/conversations` never showed the channel list at all.
     *   768–1023 Feed full-bleed; the list moves into a Sheet.
     *   ≥ 1024  Both panels inline, as wireframed.
     *
     * Both panels stay MOUNTED in every band and are hidden with `hidden` rather
     * than unmounted. Unmounting the feed under 768 would tear down the socket
     * subscription and the message query on every panel swap, so returning to a
     * channel would re-fetch and re-render from a skeleton.
     */
    <div className="flex h-full min-h-0 flex-col gap-3 lg:grid lg:grid-cols-[220px_1fr] lg:grid-rows-[minmax(0,1fr)]">
      {/* The screen's own heading. The visible <h2> below names the CHANNEL,
          which changes as you navigate — so on its own it left the screen with
          no stable <h1> to orient by, and the heading order started at 2
          (§9, TITAN-FRT-043). */}
      <h1 className="sr-only">Conversations</h1>
      <ChannelList
        channels={channels}
        activeId={active}
        className={cn(
          "min-h-0 overflow-y-auto lg:block",
          channelId
            ? "hidden lg:block"
            : // No channel chosen: the list IS the screen under 768, so it
              // takes the leftover height and scrolls inside itself rather than
              // pushing the body (§8: no horizontal or body scroll).
              "block flex-1 md:hidden lg:block lg:flex-none",
        )}
      />

      <section
        className={cn(
          "min-h-0 flex-1 flex-col rounded-lg border bg-card",
          // Hidden only in the one band where the list has the screen to itself.
          channelId ? "flex" : "hidden md:flex",
        )}
      >
        <header className="flex flex-wrap items-center gap-2 border-b p-2">
          {/* §8: "one panel at a time (channel list ↔ feed)" needs a way BACK,
              and it is deliberately the first focusable thing in the feed so a
              keyboard or screen-reader user meets it before 200 messages. A
              Link, not `router.back()`: the feed is reachable by deep link and
              from the bottom tab bar, where there is no channel list to go back
              TO. */}
          <Link
            href="/conversations"
            className="-ml-1 flex items-center gap-1 rounded-md px-2 text-sm text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none max-md:min-h-11 md:hidden"
          >
            <ChevronLeft aria-hidden className="size-4" />
            Channels
          </Link>

          {/* The 768–1023 affordance. `md:flex lg:hidden` is exactly §8's middle
              band: below it the Link above does the job, above it the list is
              resident and a trigger would be a second way to reach a panel
              already on screen. */}
          <Sheet open={channelSheet} onOpenChange={setChannelSheet}>
            <SheetTrigger className="-ml-1 hidden min-h-11 items-center gap-1 rounded-md px-2 text-sm text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none md:flex lg:hidden">
              <Hash aria-hidden className="size-4" />
              Channels
            </SheetTrigger>
            <SheetContent side="left" className="w-72 p-0">
              <SheetHeader className="px-4 py-3">
                <SheetTitle className="text-left text-sm">Channels</SheetTitle>
              </SheetHeader>
              <ChannelList
                channels={channels}
                activeId={active}
                className="mx-2 mb-2 overflow-y-auto border-0 bg-transparent"
                // Choosing a channel has to dismiss the sheet: the navigation
                // is client-side, so nothing else unmounts it and the reader
                // would be left staring at the list they just used.
                onNavigate={() => setChannelSheet(false)}
              />
            </SheetContent>
          </Sheet>

          <h2 className="truncate text-sm font-medium">{channelName}</h2>
          {connection === "live" ? (
            <Badge variant="secondary">live</Badge>
          ) : (
            <Badge variant="outline">reconnecting</Badge>
          )}
          {/* The pressed state cannot ride on the badge VARIANT alone: `review`
              and `architecture` are themselves variant "outline", so toggling
              them changed nothing on screen — the filter worked and looked
              broken. A ring plus a ✓ marks it, which also satisfies
              TITAN-FRT-044 (status is never colour-only).

              Nine chips wrapped to three rows at 375px and every one of them was
              under the 44px §8 requires. They now sit on one horizontally
              scrolling row under md — §8's own "wide content scrolls in its own
              container" — at full touch height; at md+ they wrap and go dense
              again. `shrink-0` is what makes the scroll happen instead of the
              chips being squeezed to unreadable slivers. */}
          {/* `max-md:-my-1 max-md:py-1` is not spacing — it is the focus ring's
              room. Setting `overflow-x` to anything but `visible` makes the
              UNSET `overflow-y` compute to `auto` as well, so a scroller whose
              height is exactly its 44px chips clips every chip's focus ring flat
              against the top and bottom edges (TITAN-FRT-042). The negative
              margin gives the padding back so nothing moves. */}
          <div className="-mx-2 flex w-full snap-x gap-1 overflow-x-auto px-2 max-md:-my-1 max-md:py-1 md:mx-0 md:ml-auto md:w-auto md:flex-wrap md:overflow-x-visible md:px-0">
            {MESSAGE_KINDS.map((kind) => {
              const on = kinds.has(kind);
              return (
                <button
                  key={kind}
                  type="button"
                  onClick={() => toggleKind(kind)}
                  aria-pressed={on}
                  className={cn(
                    "flex shrink-0 snap-start items-center rounded-md focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none max-md:min-h-11",
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

        {/* TITAN-FRT-043's polite activity feed. Outside the scroller on
            purpose — see the effect that fills it. */}
        <div aria-live="polite" role="status" className="sr-only">
          {announcement.text}
          {announcement.parity ? ZERO_WIDTH_SPACE : ""}
        </div>

        <Composer projectId={active} onSent={jumpToBottom} />
      </section>
    </div>
  );
}
