"use client";

/**
 * The Logs Viewer (Vol 4 §6.7, TITAN-FRT-030, ADR-FRT-05).
 *
 * A virtualized monospace list over a 50k ring buffer, fed by the Loki-backed
 * history endpoint — backward for history, forward for the tail. Everything
 * that can be decided without pixels lives in `lib/logs`, because jsdom has no
 * layout and a virtualized list renders zero rows there; this file is the
 * wiring, and is deliberately thin.
 *
 * The two deviations from §6.7, both forced and both load-bearing:
 *
 * 1. **The tail polls; it does not stream.** `log.appended` has no producer
 *    anywhere in the backend, the WS envelope has no `topic` field, and there
 *    is no `logs` category — so §6.7's `logs:{stream}` subscription would be
 *    accepted and deliver nothing, forever. Building the producer would be
 *    worse than the gap: the socket has a 512-frame send queue, no batching and
 *    no rate limit, and it SHEDS a connection that overflows — §6.7's own
 *    target of 1,000 lines/s would kill it, taking the board and notifications
 *    with it.
 * 2. **Gap markers say a time span, not "N lines".** N needs a COUNT query;
 *    the span is exact and free. See `lib/logs/gaps`.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useVirtualizer } from "@tanstack/react-virtual";
import { toast } from "sonner";

import type { LogLine } from "@titan/api-client";
import { ApiError } from "@titan/api-client";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { GapMarker } from "@/components/logs/gap-marker";
import { LogContextDrawer } from "@/components/logs/log-context-drawer";
import { LogFilterBar, FilterSummary } from "@/components/logs/log-filter-bar";
import { LogRow } from "@/components/logs/log-row";
import {
  compileRegex,
  EMPTY_FILTERS,
  filterLines,
  isErrorLevel,
  isFiltered,
} from "@/lib/logs/filters";
import { exportFilename, toNdjson } from "@/lib/logs/export";
import {
  addGap,
  buildRows,
  failGap,
  fillGap,
  loadingGap,
  openGap,
  shouldJump,
  updateGap,
  type Gap,
} from "@/lib/logs/gaps";
import {
  PAGE_SIZE,
  TAIL_INTERVAL_MS,
  nsToIsoCeil,
  nsToIsoFloor,
  toQuery,
  useLogHistory,
} from "@/lib/logs/hooks";
import { scrollTopAfterPrepend } from "@/lib/conversations/feed";
import {
  appendLines,
  EMPTY_RING,
  mergeLines,
  newest,
  oldest,
  prependLines,
  type Ring,
} from "@/lib/logs/ring";
import { spaceOutcome } from "@/lib/logs/activation";
import { parseFilters, writeFilters } from "@/lib/logs/url";
import { isBehindOverlay } from "@/lib/shortcuts/keys";
import { usePrimarySearch } from "@/lib/shortcuts/primary-search";
import { useShortcuts } from "@/lib/shortcuts/provider";
import { browserApi } from "@/lib/query/browser-client";
import { unwrap } from "@titan/api-client";

/**
 * The virtualizer's PRE-MEASUREMENT estimate only — one 20px line, the ≥md row.
 *
 * It is not the authority on row height and never was: every item below is
 * rendered with `ref={virtualizer.measureElement}`, so real heights replace this
 * as soon as a row is on screen. That path is already load-bearing — a
 * `GapMarker` is several lines tall and an expanded row is taller still, and
 * both have always ridden it — which is what makes the two-line narrow layout in
 * `log-row` (§8, legible at 375px) safe: it is one more measured height, not a
 * new class of problem. A width-aware estimate would need `matchMedia`, which
 * this app deliberately does not have; the cost of not having one is a slightly
 * jumpy scrollbar while fast-scrolling unseen rows on a phone, not overlap.
 */
const ROW_HEIGHT = 20;
/** How close to the bottom still counts as "at the bottom". */
const AT_BOTTOM_PX = 24;
/** How close to the top starts pulling in older history. */
const NEAR_TOP_PX = 200;

export function LogsScreen() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const filters = useMemo(
    () => parseFilters(new URLSearchParams(searchParams.toString())),
    [searchParams],
  );

  const [ring, setRing] = useState<Ring>(EMPTY_RING);
  const [gaps, setGaps] = useState<readonly Gap[]>([]);
  const [tailing, setTailing] = useState(true);
  const [wrap, setWrap] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [context, setContext] = useState<LogLine | null>(null);
  const [tailError, setTailError] = useState<string | null>(null);

  // §10: `/` focuses the screen's primary search/filter. The free-text search is
  // the one this screen has — the regex box is a power tool and the chips are
  // not text. Registered unconditionally, above the loading/error early returns:
  // hooks cannot be conditional, and `usePrimarySearch` no-ops while the ref is
  // still null, so `/` correctly falls through to the palette during load.
  const searchRef = useRef<HTMLInputElement>(null);
  usePrimarySearch(searchRef);

  const history = useLogHistory(filters);

  // A new filter set is a different query entirely, so the buffer it filled is
  // meaningless — keeping it would mix lines from two filters in one list. The
  // back-scan cursor goes with it: it is a position inside the OLD query's
  // result, and reusing it would page through the wrong window.
  useEffect(() => {
    setRing(EMPTY_RING);
    setGaps([]);
    olderCursor.current = null;
    setExhausted(false);
  }, [filters]);

  // Seed from history. `backward` returns newest-first; the list reads
  // oldest-first, so reverse before merging.
  useEffect(() => {
    const items = history.data?.items;
    if (!items || items.length === 0) return;
    setRing((current) => mergeLines(current, [...items].reverse()));
  }, [history.data]);

  const compiled = useMemo(() => compileRegex(filters.regex), [filters.regex]);
  const regex = compiled.ok ? compiled.regex : null;
  const regexError = compiled.ok ? null : compiled.error;

  const shown = useMemo(
    () => filterLines(ring, filters, regex),
    [ring, filters, regex],
  );
  const rows = useMemo(() => buildRows(shown, gaps), [shown, gaps]);

  // --- the tail ------------------------------------------------------------
  //
  // A plain interval rather than a react-query `refetchInterval`: each poll
  // resumes from a cursor that the PREVIOUS poll produced, so the query key
  // would change on every tick and react-query would treat each one as a fresh
  // query with its own cache entry — 3,600 of them an hour.
  const newestRef = useRef<string | null>(null);
  newestRef.current = newest(ring)?.ts_ns ?? null;
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  // WHEN the tail last succeeded — not how old the newest line is. A quiet
  // system has an old newest-line while the tail is perfectly current, so
  // deciding to jump from the line's age marks every idle project as a gap.
  const lastPollAt = useRef<number | null>(null);

  useEffect(() => {
    if (!tailing) return;
    let cancelled = false;
    // A fresh mount/restart is not an absence: start the clock now, so the
    // first poll never opens a gap over lines it is about to fetch anyway.
    lastPollAt.current = Date.now();

    const poll = async () => {
      const after = newestRef.current;
      if (after === null) return; // history has not landed yet: nothing to resume from

      // Away long enough that replaying the backlog would cost many round
      // trips (a backgrounded tab, a closed lid)? Jump to now and record
      // exactly the window we stepped over.
      const jump = shouldJump(lastPollAt.current, Date.now());
      try {
        const page = await unwrap(
          await browserApi().GET("/api/v1/logs", {
            params: {
              query: {
                ...toQuery(filtersRef.current),
                direction: jump ? "backward" : "forward",
                limit: PAGE_SIZE,
                ...(jump ? {} : { start: nsToIsoFloor(after) }),
              } as never,
            },
          }),
        );
        if (cancelled) return;
        lastPollAt.current = Date.now();
        setTailError(null);
        const items = jump ? [...page.items].reverse() : page.items;
        if (items.length === 0) return;

        if (jump) {
          const oldestNew = items[0]?.ts_ns;
          // Only a real hole: if the jump landed on lines we already hold,
          // there is nothing to mark.
          if (oldestNew && BigInt(oldestNew) > BigInt(after)) {
            setGaps((current) => addGap(current, openGap(after, oldestNew)));
          }
          setRing((current) => mergeLines(current, items));
        } else {
          setRing((current) => appendLines(current, items));
        }
      } catch (error) {
        if (cancelled) return;
        // A banner, and the buffer is retained (§6.7's error state). The global
        // handler only toasts 401/403, so a 503 would otherwise be silent.
        setTailError(
          error instanceof ApiError
            ? `${error.status}`
            : "the log stream is unreachable",
        );
      }
    };

    const handle = setInterval(poll, TAIL_INTERVAL_MS);
    void poll();
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
    // `filters` belongs here even though the poll reads it from a ref. Without
    // it a filter change never sets `cancelled`, so a poll already in flight
    // resolves AFTER the reset effect has cleared the ring and appends its
    // old-filter lines into the new filter's buffer — where they also become
    // the tail cursor, so every later poll resumes from a line that does not
    // match the filter it is querying with.
  }, [tailing, filters]);

  // --- scrolling -----------------------------------------------------------

  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 20,
    getItemKey: (index) => rows[index]?.key ?? index,
  });

  // Follow-tail: pin to the bottom while tailing.
  useEffect(() => {
    if (!tailing || rows.length === 0) return;
    virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
  }, [rows.length, tailing, virtualizer]);

  // --- older history, on demand (§6.7: "older lines fetched on demand") ----

  const [loadingOlder, setLoadingOlder] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  const olderCursor = useRef<string | null>(null);
  // The scroll position to restore once a prepend has landed. Held in a ref
  // because it is read in a layout effect after React commits the new rows.
  const anchor = useRef<{ scrollTop: number; scrollHeight: number } | null>(
    null,
  );

  const loadOlder = useCallback(async () => {
    const el = parentRef.current;
    const from = oldest(ring);
    if (!el || loadingOlder || exhausted || !from) return;

    setLoadingOlder(true);
    try {
      const page = await unwrap(
        await browserApi().GET("/api/v1/logs", {
          params: {
            query: {
              ...toQuery(filtersRef.current),
              direction: "backward",
              limit: PAGE_SIZE,
              end: nsToIsoCeil(from.ts_ns),
              ...(olderCursor.current ? { cursor: olderCursor.current } : {}),
            } as never,
          },
        }),
      );
      // `next_cursor` is `string | null | undefined` — it has a default, so it
      // can be absent from the JSON entirely. A `=== null` check would miss
      // that and back-scan forever against a window that is already exhausted.
      olderCursor.current = page.next_cursor ?? null;
      if (!page.next_cursor) setExhausted(true);
      if (page.items.length > 0) {
        // Record the anchor HERE, not before the fetch. A second of network
        // sits between them, and the tail is appending at 1 Hz the whole time
        // — an anchor taken earlier is consumed by whichever ring update
        // commits first, so the tail's append restores a scroll position
        // measured for a prepend that has not happened yet, and the reader
        // jumps. Measured immediately before the setRing that grows the list
        // upward, it can only belong to that update.
        anchor.current = {
          scrollTop: el.scrollTop,
          scrollHeight: el.scrollHeight,
        };
        // `backward` is newest-first; the list reads oldest-first.
        setRing((current) => prependLines(current, [...page.items].reverse()));
      }
    } catch {
      setExhausted(true); // do not spin: scrolling up would retry every frame
      toast.error("Couldn’t load older logs");
    } finally {
      setLoadingOlder(false);
    }
  }, [ring, loadingOlder, exhausted]);

  // Restore the reader's position after older lines are spliced in above them.
  useLayoutEffect(() => {
    const el = parentRef.current;
    if (!el || !anchor.current) return;
    el.scrollTop = scrollTopAfterPrepend(anchor.current, el.scrollHeight);
    anchor.current = null;
  }, [ring]);

  // §6.7: "follow-tail toggle (auto-pauses on scroll-up)".
  const onScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (tailing && distance > AT_BOTTOM_PX) setTailing(false);
    // Reaching the top means reading into the past — fetch what came before.
    if (el.scrollTop < NEAR_TOP_PX) void loadOlder();
  }, [tailing, loadOlder]);

  // --- keyboard (§10: Logs — Space pauses tail, `e` next ERROR) ------------

  /**
   * The last error `e` landed on, held by its line ID rather than its row
   * index.
   *
   * An index is not a position in this list — it is a position in a list that
   * moves. The tail prepends nothing but a back-scan does, eviction drops rows
   * off the front, a gap marker appears between two rows, and a filter change
   * replaces the list wholesale. Any of those shifts every index, so a stored
   * index silently starts pointing at a different line: `e` then re-visits
   * errors it already showed and skips ones it never did. An id is stable
   * across all of it, and its absence (the line was evicted or filtered away)
   * is a meaningful "start from the top" rather than a wrong answer.
   */
  const lastErrorId = useRef<string | null>(null);
  const jumpToNextError = useCallback(() => {
    const isError = (row: (typeof rows)[number]) =>
      row.kind === "line" && isErrorLevel(row.line.level);

    // -1 when the remembered line is gone, which correctly restarts the scan.
    const at = lastErrorId.current
      ? rows.findIndex((row) => row.key === lastErrorId.current)
      : -1;
    const next = rows.findIndex((row, i) => i > at && isError(row));
    // Wrap to the top rather than dead-ending at the last error.
    const target = next === -1 ? rows.findIndex(isError) : next;
    if (target === -1) {
      toast.info("No ERROR lines in the buffer");
      return;
    }
    lastErrorId.current = rows[target].key;
    setTailing(false); // jumping and then being yanked back to the tail is useless
    virtualizer.scrollToIndex(target, { align: "center" });
  }, [rows, virtualizer]);

  // Registered with the global engine instead of a `window` listener. The engine
  // owns text-entry suppression (a Space typed into the regex box no longer
  // pauses the tail) and `preventDefault`, so those two checks this screen used
  // to hand-roll are gone.
  //
  // Declared HERE rather than beside the other effects because the array is
  // built during render and closes over `jumpToNextError`: reading it above its
  // own `const` is a temporal-dead-zone throw, not a stale closure.
  //
  // TWO of the old guards do NOT survive the move, and both are re-made here:
  //
  //  - the button/link one — see `lib/logs/activation` for why the engine cannot
  //    make it for us yet and why "do nothing" would be a worse fallback;
  //  - the OVERLAY one. The engine's `isBehindOverlay` tests the EVENT TARGET,
  //    which answers "is the thing the user is aiming at hidden?" — and while
  //    the context drawer is open the answer is NO, because Radix hides the rest
  //    of the page and puts focus inside the drawer. A screen-scoped binding
  //    needs the other question, "is MY screen the one the user can see?", which
  //    is this screen's own element against the same `aria-hidden` contract —
  //    exactly what the deleted `window` listener asked. Without it `e` scrolls
  //    the list the drawer is covering and `Space` silently toggles the tail
  //    instead of scrolling the drawer.
  const screenHidden = () => isBehindOverlay(parentRef.current);

  useShortcuts([
    {
      id: "logs-pause",
      keys: "Space",
      label: "Pause / resume tail",
      group: "Logs",
      run: () => {
        const focused = document.activeElement;
        switch (spaceOutcome(focused)) {
          case "activate":
            // Replaying the click needs no overlay guard: the control is the
            // FOCUSED one, so it belongs to whatever is on top, not to us.
            if (focused instanceof HTMLElement) {
              focused.click();
            }
            return;
          case "ignore":
            return;
          default:
            if (screenHidden()) {
              return;
            }
            setTailing((t) => !t);
        }
      },
    },
    {
      id: "logs-next-error",
      keys: "e",
      label: "Jump to next ERROR",
      group: "Logs",
      run: () => {
        if (screenHidden()) {
          return;
        }
        jumpToNextError();
      },
    },
  ]);

  // --- gap fill ------------------------------------------------------------

  const loadGap = useCallback(async (gap: Gap) => {
    setGaps((current) => updateGap(current, loadingGap(gap)));
    try {
      const page = await unwrap(
        await browserApi().GET("/api/v1/logs", {
          params: {
            query: {
              ...toQuery(filtersRef.current),
              direction: "forward",
              limit: PAGE_SIZE,
              start: nsToIsoFloor(gap.afterTs),
              end: nsToIsoCeil(gap.beforeTs),
              // Resume where the last [load] stopped. A gap can hold far more
              // than one page: without this every click re-requests the SAME
              // first page, the ring de-duplicates it all away, and the only
              // visible effect is `filled` climbing by 500 while nothing new
              // ever arrives and the marker never closes.
              ...(gap.cursor ? { cursor: gap.cursor } : {}),
            } as never,
          },
        }),
      );
      // Merge, not prepend: the gap sits between lines we already hold, so
      // its fill lands in the middle. Ids make the splice duplicate-free.
      setRing((current) => mergeLines(current, page.items));
      // `?? null` because next_cursor is `string | null | undefined` — it has
      // a default, so it can be absent from the JSON entirely, and `undefined`
      // would otherwise read as "there is more" and never close the gap.
      setGaps((current) =>
        updateGap(
          current,
          fillGap(gap, page.items.length, page.next_cursor ?? null),
        ),
      );
    } catch (error) {
      setGaps((current) =>
        updateGap(
          current,
          failGap(
            gap,
            error instanceof ApiError
              ? `HTTP ${error.status}`
              : "network error",
          ),
        ),
      );
    }
  }, []);

  // --- actions -------------------------------------------------------------

  const setFilters = useCallback(
    (next: typeof filters) => {
      const params = writeFilters(
        next,
        new URLSearchParams(searchParams.toString()),
      );
      router.replace(params.toString() ? `/logs?${params}` : "/logs", {
        scroll: false,
      });
    },
    [router, searchParams],
  );

  const onExport = useCallback(() => {
    // Exports what is on screen — which is also all the client has. §6.7 calls
    // it "filtered NDJSON"; full-history export would be a backend job.
    const blob = new Blob([toNdjson(shown)], { type: "application/x-ndjson" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = exportFilename(Date.now());
    anchor.click();
    URL.revokeObjectURL(url);
  }, [shown]);

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  // --- states --------------------------------------------------------------

  if (history.isLoading) {
    return (
      <div className="space-y-2 p-4">
        <Skeleton className="h-9 w-full" />
        {Array.from({ length: 16 }, (_, i) => (
          <Skeleton key={i} className="h-4 w-full" />
        ))}
      </div>
    );
  }

  const error = history.isError ? history.error : null;
  const status = error instanceof ApiError ? error.status : null;
  // A 422 means the FILTER the user typed is invalid (an RE2-incompatible
  // regex is the usual one). Replacing the screen with an error card would
  // unmount the filter bar holding the very text they need to fix, leaving the
  // URL as the only way out. So a 422 stays inline, below a live bar; anything
  // else is environmental and does replace the screen.
  const filterRejected = status === 422;

  if (error && !filterRejected) {
    return (
      <div className="p-6">
        <div className="rounded-lg border bg-card p-6 text-sm">
          {status === 503 ? (
            <>
              <p className="font-medium">Log history is unavailable.</p>
              <p className="mt-1 text-muted-foreground">
                No Loki backend is configured. Loki runs under the observability
                profile — start it with{" "}
                <code className="font-mono text-xs">
                  docker compose --profile obs up
                </code>{" "}
                and set{" "}
                <code className="font-mono text-xs">
                  TITAN_LOKI_URL=http://loki:3100
                </code>
                .
              </p>
            </>
          ) : status === 403 ? (
            <>
              <p className="font-medium">
                You don’t have access to the system logs.
              </p>
              <p className="mt-1 text-muted-foreground">
                Reading logs needs the{" "}
                <code className="font-mono text-xs">logs:read</code> permission,
                which is granted to owners and admins only.
              </p>
            </>
          ) : (
            <p className="text-destructive">
              Couldn’t load the logs. {String(error)}
            </p>
          )}
          <Button
            className="mt-4"
            size="sm"
            variant="outline"
            onClick={() => history.refetch()}
          >
            Retry
          </Button>
        </div>
      </div>
    );
  }

  return (
    // `h-full`, not `h-[calc(100vh-4rem)]`. The old arithmetic subtracted a
    // guess at the topbar from `100vh` — which on mobile is the LARGE viewport
    // (the shell uses `h-dvh` precisely because `vh` ignores retracting browser
    // chrome) and budgeted nothing for the `< 768` bottom tab bar, so the last
    // rows of the list sat underneath it. `<main>` is already the right box and
    // already carries the bar's clearance; `min-h-0` lets the list shrink below
    // its content so it scrolls internally instead of scrolling the body (§8).
    <div className="flex h-full min-h-0 flex-col">
      {/* A filter bar over a virtualized list, with no visible title — and so,
          until Phase 65, no heading at all. A screen with no <h1> leaves a
          screen-reader user nothing to orient by on arrival and nothing to
          find when navigating by heading (§9, TITAN-FRT-043). `sr-only` keeps
          the wireframe exactly as designed. */}
      <h1 className="sr-only">Logs</h1>
      <LogFilterBar
        filters={filters}
        onChange={setFilters}
        searchRef={searchRef}
        tailing={tailing}
        onToggleTail={() => setTailing((t) => !t)}
        wrap={wrap}
        onToggleWrap={() => setWrap((w) => !w)}
        onExport={onExport}
        shown={shown.length}
        regexError={regexError}
      />

      {filterRejected ? (
        <p
          role="status"
          className="border-b bg-destructive/10 px-2 py-1 text-xs text-destructive"
        >
          The server rejected this filter:{" "}
          {error instanceof ApiError
            ? (error.problem?.detail ?? error.message)
            : "invalid filter"}
          . Edit it above — a regex that works here can still be rejected,
          because Loki uses RE2 and has no lookahead or backreferences.
        </p>
      ) : null}

      {tailError ? (
        <p
          role="status"
          className="border-b bg-muted/50 px-2 py-1 text-xs text-muted-foreground"
        >
          Live tail interrupted ({tailError}) — showing the lines already
          loaded.{" "}
          <button
            type="button"
            className="underline"
            onClick={() => setTailError(null)}
          >
            Dismiss
          </button>
        </p>
      ) : null}

      {rows.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-sm text-muted-foreground">
          <p>
            {isFiltered(filters)
              ? "No log lines match filters"
              : "No log lines yet."}
          </p>
          {isFiltered(filters) ? (
            <>
              <FilterSummary filters={filters} />
              <Button
                size="sm"
                variant="outline"
                onClick={() => setFilters(EMPTY_FILTERS)}
              >
                Reset filters
              </Button>
            </>
          ) : null}
        </div>
      ) : (
        <div
          ref={parentRef}
          onScroll={onScroll}
          // `min-h-0`: a flex item's default `min-height: auto` refuses to
          // shrink below its content, so the virtualizer's multi-thousand-pixel
          // spacer would push the container past the viewport and scroll the
          // BODY instead of this box — §8's one absolute rule.
          className="min-h-0 flex-1 overflow-auto"
        >
          {loadingOlder ? (
            <p
              className="py-1 text-center text-[11px] text-muted-foreground"
              role="status"
            >
              Loading older lines…
            </p>
          ) : null}
          <div
            className="relative w-full"
            style={{ height: virtualizer.getTotalSize() }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              const row = rows[item.index];
              return (
                <div
                  key={item.key}
                  ref={virtualizer.measureElement}
                  data-index={item.index}
                  className="absolute left-0 top-0 w-full"
                  style={{ transform: `translateY(${item.start}px)` }}
                >
                  {row.kind === "gap" ? (
                    <GapMarker gap={row.gap} onLoad={loadGap} />
                  ) : (
                    <LogRow
                      line={row.line}
                      filters={filters}
                      regex={regex}
                      wrap={wrap}
                      expanded={expanded.has(row.line.id)}
                      onToggle={toggleExpanded}
                      onOpenContext={setContext}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <LogContextDrawer
        line={context}
        ring={ring}
        onClose={() => setContext(null)}
      />
    </div>
  );
}
