"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Radio } from "lucide-react";

import { isDomainFrame, type ServerFrame } from "@titan/ws-protocol";

import { CONNECTION_PRESENTATION } from "@/lib/ws/connection-presentation";
import { useConnection } from "@/lib/ws/connection-store";
import { useWsClient } from "@/lib/ws/manager";
import { createRafBatcher } from "@/lib/ws/raf-batch";
import { cn } from "@/lib/utils";

/**
 * The §6.1 live-activity feed: virtualized, newest-first, prepending with a
 * 200 ms highlight, rows linking to their entity.
 *
 * HONEST SCOPE: there is no activity-history endpoint. The backend's
 * `org:activity` frame carries only `{event, project_id}` — no actor, no verb,
 * no entity title — and nothing persists a readable feed. So this is a LIVE-ONLY
 * feed: it starts empty and fills as frames arrive for this session; it cannot
 * backfill on load, and its rows say what the wire actually carries rather than
 * inventing "QA-1 opened bug TASK-231". The wireframe's richer rows and its
 * `…infinite scroll…` history need a backend feed endpoint.
 *
 * Frames are batched through requestAnimationFrame (TITAN-FRT-021) so a burst
 * can't cause a render per event.
 */
const MAX_ROWS = 500; // ring buffer — the feed is live-only, so it can't grow forever
const HIGHLIGHT_MS = 200;

/**
 * How often the polite live region may change (TITAN-FRT-043).
 *
 * The feed is RAF-batched, so without this the announcement text could change
 * ~60×/s under a burst. A polite region that mutates that fast is worse than
 * none: some screen readers queue every revision and fall minutes behind the
 * UI, others drop the lot. Throttling on the trailing edge means the region
 * settles on "what just arrived, and how much of it" a few times a second at
 * most — which is the only thing a listener can actually use anyway.
 */
const ANNOUNCE_THROTTLE_MS = 2_000;

export interface ActivityRow {
  readonly id: string;
  readonly ts: string;
  readonly type: string;
  readonly projectId: string | null;
}

function timeOf(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "--:--"
    : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/**
 * Frame -> a row.
 *
 * The org channel wraps the real event: the backend publishes
 * `activity.created` with `payload = {event, project_id}` and does NOT set the
 * envelope's `project_id` (it is always null on that channel). So the row's name
 * and link come from the PAYLOAD for org frames, and from the envelope for the
 * per-project frames a project route subscribes to.
 */
export function toRow(frame: ServerFrame): ActivityRow | null {
  if (!isDomainFrame(frame)) {
    return null;
  }
  if (frame.type === "run.output.delta") {
    return null; // token noise, not activity
  }

  let type: string = frame.type;
  let projectId = frame.project_id;
  if (frame.type === "activity.created") {
    const payload = frame.payload as {
      event?: unknown;
      project_id?: unknown;
    } | null;
    if (typeof payload?.event === "string") {
      type = payload.event;
    }
    if (typeof payload?.project_id === "string") {
      projectId = payload.project_id;
    }
  }

  return {
    id: `${frame.type}:${projectId ?? "org"}:${frame.seq}:${frame.ts}`,
    ts: frame.ts,
    type,
    projectId,
  };
}

export function ActivityFeed({ rows: injected }: { rows?: ActivityRow[] }) {
  const client = useWsClient();
  const { status } = useConnection();
  const presentation = CONNECTION_PRESENTATION[status];
  const [rows, setRows] = useState<ActivityRow[]>(injected ?? []);
  const [freshIds, setFreshIds] = useState<ReadonlySet<string>>(new Set());
  const [announcement, setAnnouncement] = useState("");
  const parentRef = useRef<HTMLDivElement>(null);
  const headingId = useId();

  useEffect(() => {
    if (injected || !client) {
      return;
    }
    let highlightTimer: ReturnType<typeof setTimeout> | undefined;
    // Trailing-edge throttle for the polite region (see ANNOUNCE_THROTTLE_MS).
    // `pending` accumulates across bursts so nothing is silently dropped — the
    // count is what tells a listener they missed traffic.
    let announceTimer: ReturnType<typeof setTimeout> | undefined;
    let pendingCount = 0;
    let pendingLatest: ActivityRow | undefined;
    const flushAnnouncement = () => {
      announceTimer = undefined;
      if (!pendingLatest) {
        return;
      }
      const extra = pendingCount - 1;
      setAnnouncement(
        extra > 0
          ? `${pendingLatest.type}, and ${extra} more ${extra === 1 ? "update" : "updates"}`
          : pendingLatest.type,
      );
      pendingCount = 0;
      pendingLatest = undefined;
    };
    const batcher = createRafBatcher<ActivityRow>((incoming) => {
      // Newest-first. `toReversed` (not `reverse`) because a React updater must
      // be pure — StrictMode double-invokes it, and mutating the captured array
      // would undo the reversal on the second pass.
      const newestFirst = [...incoming].reverse();
      setRows((prev) => [...newestFirst, ...prev].slice(0, MAX_ROWS));
      setFreshIds(new Set(newestFirst.map((r) => r.id)));
      // The 200 ms prepend highlight (§6.1). Restart the timer per burst: a
      // single untracked timer from an earlier burst would wipe a newer burst's
      // highlight early (under a sustained stream, after ~one frame).
      clearTimeout(highlightTimer);
      highlightTimer = setTimeout(() => setFreshIds(new Set()), HIGHLIGHT_MS);

      pendingCount += newestFirst.length;
      pendingLatest = newestFirst[0];
      // Only ARM the timer — never restart it. A restart on every burst would
      // starve the region completely under a sustained stream: the deadline
      // would keep moving and the user would be told nothing at all while the
      // most traffic was flowing, which is exactly backwards.
      announceTimer ??= setTimeout(flushAnnouncement, ANNOUNCE_THROTTLE_MS);
    });
    const unsubscribe = client.onAnyFrame((frame) => {
      const row = toRow(frame);
      if (row) {
        batcher.push(row);
      }
    });
    return () => {
      unsubscribe();
      batcher.dispose();
      clearTimeout(highlightTimer);
      clearTimeout(announceTimer);
    };
  }, [client, injected]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 32,
    overscan: 8,
  });

  const items = virtualizer.getVirtualItems();
  const empty = useMemo(() => rows.length === 0, [rows.length]);

  return (
    <div className="flex h-full flex-col rounded-lg border bg-card">
      {/*
        TITAN-FRT-043's "activity feed aria-live=polite" lives HERE and not on
        the list, because the list is virtualized (see role="log" below). This
        region holds exactly one short sentence describing the newest arrival,
        it is never recycled, and it is `aria-atomic` so the whole sentence is
        read rather than the diff. `sr-only` — the sighted equivalent is the
        200 ms prepend highlight the rows already have.
      */}
      <div aria-live="polite" aria-atomic className="sr-only">
        {announcement}
      </div>

      <div className="flex items-center gap-2 border-b px-3 py-2">
        {/*
          TITAN-FRT-044. This icon used to be a hardcoded `text-success` beside
          a static "Live activity" label: it stayed green while the socket was
          reconnecting or offline, so the one thing on screen that claimed the
          feed was live was the one thing that could never know. It now reads
          the real status, and the status is spelled out in TEXT next to it —
          colour was previously the only channel, which §9 forbids outright.

          The label and dot colour come from CONNECTION_PRESENTATION in lib/ws,
          shared with the top-bar pill, so the two never word the same state
          differently ("Reconnecting…" here, "reconnecting" there).
        */}
        <Radio aria-hidden className={cn("size-3.5", presentation.icon)} />
        <span id={headingId} className="text-sm font-semibold">
          Live activity
        </span>
        {/*
          NOT a live region, deliberately. <ConnectionPill> is mounted in the top
          bar on every screen and already carries `role="status"` for exactly
          this value; adding a second one here would make a screen reader
          announce every reconnect twice. This copy is for the eyes — it is the
          non-colour channel §9 requires beside the dot.
        */}
        <span className="ml-auto text-xs text-muted-foreground">
          {presentation.label}
        </span>
      </div>

      {empty ? (
        <p className="px-3 py-8 text-center text-sm text-muted-foreground">
          Nothing yet — activity appears here as agents work.
          <span className="mt-1 block text-xs">
            This feed is live-only: the backend keeps no activity history to
            load.
          </span>
        </p>
      ) : (
        <div
          ref={parentRef}
          // TITAN-FRT-043. `role="log"` gives this scroller its correct
          // semantics — an append-only running record, which assistive tech
          // exposes as a navigable landmark-ish container.
          //
          // `aria-live="off"` is NOT redundant and must not be deleted. In ARIA,
          // `role="log"` carries an IMPLICIT `aria-live="polite"`; simply
          // omitting the attribute leaves the live region switched ON. That is
          // fatal here because the list is VIRTUALIZED: @tanstack/react-virtual
          // mounts and unmounts row nodes as the window moves, so every scroll
          // tick and every re-measure is a burst of DOM insertions that a screen
          // reader would read out as new activity — re-announcing rows the user
          // has already heard, for content that did not change. Only an explicit
          // `off` overrides a role's implicit live value.
          //
          // Real arrivals are announced by the separate, non-virtualized polite
          // region above, which holds one sentence and recycles nothing.
          role="log"
          aria-live="off"
          aria-labelledby={headingId}
          className="max-h-80 flex-1 overflow-y-auto"
        >
          <div
            style={{ height: virtualizer.getTotalSize(), position: "relative" }}
          >
            {items.map((item) => {
              const row = rows[item.index];
              const inner = (
                <span className="flex items-center gap-2 truncate">
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">
                    {timeOf(row.ts)}
                  </span>
                  <span className="truncate">{row.type}</span>
                </span>
              );
              return (
                <div
                  key={row.id}
                  data-fresh={freshIds.has(row.id) ? "" : undefined}
                  className={cn(
                    "absolute top-0 left-0 flex w-full items-center px-3 text-sm transition-colors",
                    freshIds.has(row.id) && "bg-accent",
                  )}
                  style={{
                    height: item.size,
                    transform: `translateY(${item.start}px)`,
                    transitionDuration: `${HIGHLIGHT_MS}ms`,
                  }}
                >
                  {row.projectId ? (
                    <Link
                      href={`/projects/${row.projectId}`}
                      className="truncate hover:underline"
                    >
                      {inner}
                    </Link>
                  ) : (
                    inner
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
