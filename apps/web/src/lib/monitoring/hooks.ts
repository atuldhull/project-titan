"use client";

import { useQuery } from "@tanstack/react-query";

import { unwrap, type UsageSummary } from "@titan/api-client";

import {
  RANGES,
  rangeStart,
  type GroupBy,
  type RangeKey,
} from "@/lib/monitoring/usage";
import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

/**
 * The LLM cost ledger over a range (TITAN-FRT-033).
 *
 * §6.10 wants a 1 Hz `metric.sample` frame rolling the tiles live. It does not
 * exist — zero producers, and the only two Python matches for the name are
 * `prometheus_client`'s own `metric.samples` attribute. Nor is there anything
 * for it to carry: p95 latency, error rate and queue depth live in Prometheus
 * text on `/metrics` (`include_in_schema=False`, absent from the contract), and
 * nothing bridges Prometheus into the app. So the tiles refetch on an interval,
 * which is honest and is also the right shape for an aggregate that changes
 * when a call completes rather than continuously.
 */
const REFRESH_MS = 15_000;

export function useUsage(
  range: RangeKey,
  groupBy: GroupBy,
  projectId?: string | null,
  nowMs?: number,
) {
  const now = nowMs ?? Date.now();
  const start = rangeStart(range, now).toISOString();
  const bucket = RANGES[range].bucket;

  return useQuery({
    // `start` is deliberately NOT in the key: it moves every millisecond, so
    // keying on it would mint a new cache entry per render and refetch forever.
    // The range name is the stable identity of "the last hour".
    queryKey: queryKeys.usage({ range, groupBy, projectId: projectId ?? null }),
    queryFn: async (): Promise<UsageSummary> =>
      unwrap(
        await browserApi().GET("/api/v1/usage", {
          params: {
            query: {
              start,
              group_by: groupBy,
              bucket,
              ...(projectId ? { project_id: projectId } : {}),
            } as never,
          },
        }),
      ),
    refetchInterval: REFRESH_MS,
    // The window ends at "now", so it genuinely changes; unlike log history it
    // must not be served stale forever.
    staleTime: 0,
    retry: false,
  });
}
