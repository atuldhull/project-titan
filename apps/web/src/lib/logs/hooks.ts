"use client";

/**
 * Data access for the Logs Viewer.
 *
 * **Why the tail polls instead of streaming.** §6.7 specifies `log.appended`
 * batches over the WS on a `logs:{stream}` topic. None of that exists: the
 * frame type has zero producers anywhere in the backend, the envelope has no
 * `topic` field at all, and there is no `logs` category — subscribing to
 * "logs:agents" is silently accepted and delivers nothing forever. Vol 4
 * §5.2's whole topic table is aspirational (`agent.heartbeat` and
 * `message.chunk` were found absent the same way in Phase 61).
 *
 * Building the missing producer would also be the wrong move, not just extra
 * work: the socket has a 512-frame send queue, does no server-side batching and
 * has no rate limiting, and a connection that overflows is marked dead and
 * shed. Pushing §6.7's own target of 1,000 lines/s through it would kill the
 * socket — taking the board, the agent grid and notifications down with it —
 * within a second.
 *
 * So the tail polls the same history endpoint the rest of the screen uses,
 * forward from its cursor. That is slower to first byte (a second, not a
 * frame), and it is honest, exact and duplicate-free: the tail and the history
 * fill share one cursor, one dedupe and one source of truth.
 */
import { useQuery } from "@tanstack/react-query";

import { unwrap, type LogPage } from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import type { LogFilters } from "@/lib/logs/filters";

/** How often the tail asks for what is new. */
export const TAIL_INTERVAL_MS = 1_000;

/** Lines per request. The endpoint's ceiling is 4999 (one under Loki's own). */
export const PAGE_SIZE = 500;

/**
 * How far back the opening page looks.
 *
 * The endpoint defaults to Loki's own one-hour window when given no bounds,
 * which is wrong for a viewer whose whole job is "show me the last N lines": a
 * system that was quiet for an hour opens EMPTY, and empty is not merely
 * unhelpful — with no lines there is no oldest line, so the back-scan has
 * nothing to page from and the reader cannot reach the logs that do exist. A
 * day covers an overnight gap and stays far inside Loki's `max_query_length`
 * (721h) and this deployment's 14-day retention.
 */
export const OPENING_WINDOW_MS = 24 * 60 * 60 * 1_000;

/** The query params the endpoint takes, built from the filter bar. */
export type LogQuery = Readonly<
  Record<string, string | number | string[] | undefined>
>;

export function toQuery(filters: LogFilters): LogQuery {
  return {
    // `undefined` is dropped by the client; `null` would serialize as "null"
    // and filter for the literal string.
    service: filters.services.length > 0 ? [...filters.services] : undefined,
    level: filters.levels.length > 0 ? [...filters.levels] : undefined,
    project_id: filters.projectId ?? undefined,
    task_id: filters.taskId ?? undefined,
    agent_id: filters.agentId ?? undefined,
    search: filters.search ?? undefined,
    regex: filters.regex ?? undefined,
  };
}

async function fetchLogs(query: Record<string, unknown>): Promise<LogPage> {
  return unwrap(
    await browserApi().GET("/api/v1/logs", {
      params: { query: query as never },
    }),
  );
}

/**
 * The newest page of history — what the screen opens on.
 *
 * `backward` because "the last N lines" is what a log viewer shows first; the
 * screen reverses it into reading order.
 */
export function useLogHistory(filters: LogFilters, enabled = true) {
  const query = toQuery(filters);
  return useQuery({
    queryKey: queryKeys.logs({ ...query, dir: "backward" }),
    queryFn: () =>
      fetchLogs({
        ...query,
        direction: "backward",
        limit: PAGE_SIZE,
        // `start` is resolved inside the queryFn, not in the key: baking a
        // `Date.now()` into the key would mint a new cache entry on every
        // render and refetch forever.
        start: new Date(Date.now() - OPENING_WINDOW_MS).toISOString(),
      }),
    enabled,
    // History is immutable: a window that has passed cannot change. Refetching
    // it would only re-download identical bytes.
    staleTime: Infinity,
    // A 503 means Loki is not deployed (it is Compose-profile-gated), and a 403
    // means this user is not an admin. Neither improves by asking again.
    retry: false,
  });
}

/**
 * The tail, the gap fill and the back-scan are NOT hooks.
 *
 * Each resumes from a cursor the PREVIOUS call produced, so a `useQuery` key
 * would change on every step and react-query would mint a fresh cache entry per
 * poll — 3,600 an hour, none of them ever read again. They are plain awaited
 * calls in the screen against the same endpoint; only the opening page, whose
 * key is a pure function of the filters, is cached.
 */

/**
 * A Loki nanosecond as the RFC 3339 the endpoint's `start`/`end` take.
 *
 * The endpoint speaks datetimes and a datetime is MICROSECOND-resolution, so
 * the bottom three digits of a nanosecond cannot survive the trip. Which way we
 * round is therefore a correctness decision, not a tidiness one, and it differs
 * per bound:
 *
 * - **start floors.** Ceiling it would step PAST any line sitting between the
 *   boundary and the next microsecond — e.g. resuming after a line at
 *   ...000_000_500 would start at ...000_001_000 and silently skip a line at
 *   ...000_000_700.
 * - **end ceils.** Flooring it would end the window BEFORE lines between the
 *   floor and the boundary, losing them the same way.
 *
 * Both directions widen the window, so both can re-fetch a line we already
 * hold. That costs nothing — the ring de-duplicates by id — whereas narrowing
 * it drops lines with no trace, which is the exact failure this screen exists
 * to avoid.
 */
/* `BigInt(1_000)` rather than the `1_000n` literal: apps/web targets ES2017,
   where the literal syntax is a compile error. The types are available (`lib`
   includes esnext), so only the sugar is missing. */
const THOUSAND = BigInt(1_000);
const ZERO = BigInt(0);
const ONE = BigInt(1);

export function nsToIsoFloor(tsNs: string): string {
  return microsToIso(BigInt(tsNs) / THOUSAND);
}

export function nsToIsoCeil(tsNs: string): string {
  const ns = BigInt(tsNs);
  return microsToIso(ns / THOUSAND + (ns % THOUSAND === ZERO ? ZERO : ONE));
}

function microsToIso(us: bigint): string {
  const ms = Number(us / THOUSAND);
  const remainderUs = Number(us % THOUSAND);
  // `new Date(ms).toISOString()` renders milliseconds; the remaining three
  // digits of microsecond precision are appended by hand.
  const base = new Date(ms).toISOString(); // ...THH:MM:SS.mmmZ
  return `${base.slice(0, -1)}${String(remainderUs).padStart(3, "0")}Z`;
}
