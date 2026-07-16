"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Radio } from "lucide-react";

import { isDomainFrame, type ServerFrame } from "@titan/ws-protocol";

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
  const [rows, setRows] = useState<ActivityRow[]>(injected ?? []);
  const [freshIds, setFreshIds] = useState<ReadonlySet<string>>(new Set());
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (injected || !client) {
      return;
    }
    let highlightTimer: ReturnType<typeof setTimeout> | undefined;
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
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <Radio aria-hidden className="size-3.5 text-success" />
        <span className="text-sm font-semibold">Live activity</span>
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
        <div ref={parentRef} className="max-h-80 flex-1 overflow-y-auto">
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
