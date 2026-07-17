"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  unwrap,
  type MemoryBrowsePage,
  type MemoryFacets,
  type MemoryRecordDetail,
  type MemorySearchResult,
} from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

export interface BrowseFilters {
  readonly scope?: string;
  readonly subtype?: string;
  readonly kind?: string;
  readonly sourceAgent?: string;
  readonly q?: string;
}

function toQuery(filters: BrowseFilters): Record<string, string> {
  const query: Record<string, string> = {};
  if (filters.scope) query.scope = filters.scope;
  if (filters.subtype) query.subtype = filters.subtype;
  if (filters.kind) query.kind = filters.kind;
  if (filters.sourceAgent) query.source_agent = filters.sourceAgent;
  if (filters.q) query.q = filters.q;
  return query;
}

const PAGE = 50;

/**
 * The browse list, paged by the backend's keyset cursor.
 *
 * `useInfiniteQuery`, not a single `useQuery` with a hardcoded `limit`: the
 * first version capped at 50 and discarded `next_cursor`, so records past the
 * newest 50 were simply unreachable. The endpoint returns the `{items,
 * next_cursor}` envelope precisely so the list can page; this consumes it.
 */
export function useMemoryRecords(filters: BrowseFilters) {
  const query = toQuery(filters);
  return useInfiniteQuery({
    queryKey: queryKeys.memoryRecords(query),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }): Promise<MemoryBrowsePage> =>
      unwrap(
        await browserApi().GET("/api/v1/memory/records", {
          params: {
            query: {
              ...query,
              limit: PAGE,
              cursor: pageParam ?? undefined,
            } as never,
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
}

export function useMemoryFacets(scope?: string) {
  const query = scope ? { scope } : {};
  return useQuery({
    queryKey: queryKeys.memoryFacets(query),
    queryFn: async (): Promise<MemoryFacets> =>
      unwrap(
        await browserApi().GET("/api/v1/memory/facets", {
          params: { query: query as never },
        }),
      ),
    retry: false,
  });
}

/**
 * One record with its chunks and its complete ledger trail (TITAN-FRT-034).
 *
 * Cached indefinitely by id: a memory record is effectively immutable — a
 * revision creates a NEW record and points the old one's `superseded_by` at it
 * rather than editing in place — and the ledger is append-only. Re-reading is
 * only worth it after a curation action, which invalidates explicitly.
 */
export function useMemoryRecord(recordId: string | null) {
  return useQuery({
    queryKey: queryKeys.memoryRecord(recordId ?? ""),
    queryFn: async (): Promise<MemoryRecordDetail> =>
      unwrap(
        await browserApi().GET("/api/v1/memory/records/{record_id}", {
          params: { path: { record_id: recordId! } },
        }),
      ),
    enabled: recordId !== null,
    retry: false,
  });
}

/**
 * Semantic search (§6.11).
 *
 * Gated on `query.length`, NOT run via an imperative `refetch()`. That looks
 * like a smaller difference than it is: `query` is the *submitted* term (the
 * live input is separate state), so it changes only on Enter — which means
 * keying on it fires the search exactly when the spec wants and re-fires when a
 * new term is submitted, with no keystroke fetches.
 *
 * The imperative version had a real bug: the screen did `setQuery(next); if
 * (next) search.refetch()`, but a state update is async, so `refetch()` ran
 * against the query from the PREVIOUS render — the first Enter always searched
 * the empty string, rendering a false "nothing is embedded" for a term that
 * was never sent. Driving off the key removes the race entirely.
 *
 * `staleTime: Infinity` because semantic search is deterministic: clicking
 * through results must not re-embed and re-search the same term.
 */
export function useMemorySearch(query: string, scope: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: queryKeys.memorySearch(trimmed, scope),
    queryFn: async (): Promise<MemorySearchResult> =>
      unwrap(
        await browserApi().POST("/api/v1/memory/records:search", {
          body: { query: trimmed, scope } as never,
        }),
      ),
    enabled: trimmed.length > 0,
    staleTime: Infinity,
    retry: false,
  });
}
