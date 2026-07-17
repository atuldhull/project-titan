"use client";

/**
 * Memory Explorer (Vol 4 §6.11, TITAN-FRT-034).
 *
 * What is real, and what §6.11 asks for that is not:
 *
 * - **Real**: the tier tree (as two crossing axes rather than one — see
 *   `lib/memory/tiers`), faceted browse, semantic search with a score that
 *   means something, and — the MUST — the full provenance chain and usage
 *   history, which the backend has always returned together.
 * - **Not real — curation.** §6.11 wants Pin, Edit tags and Archive. There is
 *   no per-record endpoint for any of them: curation is a batch job
 *   (`POST /memory/admin/curation/run`) that decays and prunes on its own
 *   schedule, and pin/archive exist as COLUMN states no route can set. The
 *   actions are shown disabled with that reason rather than as buttons that
 *   silently do nothing.
 * - **Not real — `memory.updated`.** Zero producers; the memory module writes
 *   no domain events at all (its "outbox" is the embedding pipeline, not the
 *   event stream). §6.11's "3 new" tier badge has nothing to count, so the
 *   list is refreshed by the user, which §6.11 wanted anyway ("refresh on
 *   demand, never mutate the list under a reading user").
 */
import { useMemo, useState } from "react";
import { Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { MemoryDetail } from "@/components/memory/memory-detail";
import {
  useMemoryFacets,
  useMemoryRecords,
  useMemorySearch,
} from "@/lib/memory/hooks";
import { buildTiers, emptyTierCopy, type Tier } from "@/lib/memory/tiers";
import { cn } from "@/lib/utils";

export function MemoryScreen() {
  const [tier, setTier] = useState<Tier | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");

  const facets = useMemoryFacets();
  const tiers = buildTiers(facets.data);
  const records = useMemoryRecords({
    scope: tier?.filter?.scope,
    subtype: tier?.filter?.subtype,
  });
  const scope = tier?.filter?.scope ?? "org";
  const search = useMemorySearch(query, scope);

  const searching = query.length > 0;
  const rows = useMemo(
    () => (records.data?.pages ?? []).flatMap((p) => p.items),
    [records.data],
  );
  const hits = search.data?.hits ?? [];

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      <header className="flex flex-wrap items-center gap-3 border-b p-3">
        <h1 className="text-sm font-semibold">Memory Explorer</h1>
        <form
          className="relative flex-1 max-w-lg"
          onSubmit={(e) => {
            e.preventDefault();
            // Just set the submitted query. The search hook is gated on it, so
            // the fetch fires when this state commits — no imperative refetch,
            // which would run against the query from the PREVIOUS render and
            // search the empty string on the first Enter.
            setQuery(draft.trim());
          }}
        >
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="semantic search: “auth token refresh”"
            aria-label="Semantic search"
            className="h-8 pl-7 text-xs"
          />
        </form>
        {searching ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setQuery("");
              setDraft("");
            }}
          >
            Clear search
          </Button>
        ) : null}
      </header>

      <div className="flex min-h-0 flex-1">
        <nav
          className="w-56 shrink-0 space-y-1 border-r p-2"
          aria-label="Memory tiers"
        >
          {facets.isLoading ? (
            <Skeleton className="h-24" />
          ) : (
            tiers.map((t) => (
              <button
                key={t.id}
                type="button"
                disabled={t.kind === "unavailable"}
                aria-pressed={tier?.id === t.id}
                title={t.hint}
                onClick={() => {
                  setTier(tier?.id === t.id ? null : t);
                  setSelected(null);
                }}
                className={cn(
                  "flex w-full items-center justify-between rounded px-2 py-1 text-left text-xs",
                  tier?.id === t.id
                    ? "bg-secondary font-medium"
                    : "hover:bg-muted",
                  t.kind === "unavailable" && "cursor-not-allowed opacity-50",
                )}
              >
                <span>{t.label}</span>
                {t.count !== null ? (
                  <Badge variant="secondary" className="h-4 px-1 text-[10px]">
                    {t.count}
                  </Badge>
                ) : (
                  <span className="text-[10px] text-muted-foreground">n/a</span>
                )}
              </button>
            ))
          )}
        </nav>

        <section
          className="min-w-0 flex-1 overflow-auto p-2"
          aria-label="Results"
        >
          {searching ? (
            <SearchResults
              loading={search.isFetching}
              error={search.isError}
              hits={hits}
              // Say WHAT was searched. Semantic search is org + at most one
              // project (BRN-003), so when the browse pane is showing every
              // scope, the search beside it is still org-only — a mismatch that
              // is confusing when silent. Naming the scope removes the silence.
              scope={scope}
              belowThreshold={search.data?.below_threshold ?? false}
              minScore={search.data?.min_score ?? 0.6}
              selected={selected}
              onSelect={setSelected}
            />
          ) : records.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          ) : records.isError ? (
            <p className="p-4 text-sm text-destructive">
              Couldn’t load memory records.{" "}
              <button className="underline" onClick={() => records.refetch()}>
                Retry
              </button>
            </p>
          ) : rows.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {emptyTierCopy(tier)}
            </p>
          ) : (
            <ul className="space-y-1">
              {rows.map((r) => (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(r.id)}
                    aria-pressed={selected === r.id}
                    className={cn(
                      "w-full rounded border p-2 text-left text-xs hover:bg-muted",
                      selected === r.id && "border-primary bg-muted",
                    )}
                  >
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium">{r.title}</span>
                      <Badge
                        variant="outline"
                        className="h-4 shrink-0 px-1 text-[10px]"
                      >
                        {r.kind}
                      </Badge>
                      {r.record_subtype ? (
                        <Badge
                          variant="secondary"
                          className="h-4 shrink-0 px-1 text-[10px]"
                        >
                          {r.record_subtype}
                        </Badge>
                      ) : null}
                    </span>
                    <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
                      {r.source_agent} ·{" "}
                      {new Date(r.created_at).toLocaleString()} · used{" "}
                      {r.access_count}×
                    </span>
                  </button>
                </li>
              ))}
              {records.hasNextPage ? (
                <li>
                  <Button
                    size="sm"
                    variant="outline"
                    className="w-full text-xs"
                    disabled={records.isFetchingNextPage}
                    onClick={() => void records.fetchNextPage()}
                  >
                    {records.isFetchingNextPage ? "Loading…" : "Load more"}
                  </Button>
                </li>
              ) : null}
            </ul>
          )}
        </section>

        <aside
          className="w-[28rem] shrink-0 overflow-auto border-l"
          aria-label="Detail"
        >
          <MemoryDetail recordId={selected} />
        </aside>
      </div>
    </div>
  );
}

function SearchResults({
  loading,
  error,
  hits,
  scope,
  belowThreshold,
  minScore,
  selected,
  onSelect,
}: {
  readonly loading: boolean;
  readonly error: boolean;
  readonly hits: readonly {
    record_id: string;
    chunk_id: string;
    score: number;
    title: string;
    kind: string;
    excerpt: string;
    source_agent: string;
  }[];
  readonly scope: string;
  readonly belowThreshold: boolean;
  readonly minScore: number;
  readonly selected: string | null;
  readonly onSelect: (id: string) => void;
}) {
  const scopeLabel = (
    <p className="px-1 pb-1 text-[10px] text-muted-foreground">
      semantic search · scope: <span className="font-mono">{scope}</span>
    </p>
  );
  if (loading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-12" />
        ))}
      </div>
    );
  }
  if (error) {
    return <p className="p-4 text-sm text-destructive">Search failed.</p>;
  }
  if (hits.length === 0) {
    return (
      <>
        {scopeLabel}
        <p className="p-6 text-sm text-muted-foreground">
          {belowThreshold
            ? // §6.11's exact copy. It is only sensible advice when there WERE
              // candidates, which is why the backend distinguishes the two.
              `No semantic matches ≥ ${minScore} — try broader phrasing.`
            : "Nothing in the index matched. Nothing has been embedded for this scope yet."}
        </p>
      </>
    );
  }
  return (
    <ul className="space-y-1">
      {scopeLabel}
      {hits.map((h) => (
        <li key={h.chunk_id}>
          <button
            type="button"
            onClick={() => onSelect(h.record_id)}
            aria-pressed={selected === h.record_id}
            className={cn(
              "w-full rounded border p-2 text-left text-xs hover:bg-muted",
              selected === h.record_id && "border-primary bg-muted",
            )}
          >
            <span className="flex items-center justify-between gap-2">
              <span className="truncate font-medium">{h.title}</span>
              {/* Qdrant's cosine — comparable between queries, which is what
                  makes the ≥0.6 floor above mean anything. */}
              <span className="shrink-0 font-mono tabular-nums text-muted-foreground">
                {h.score.toFixed(2)}
              </span>
            </span>
            <span className="mt-0.5 block line-clamp-2 text-[11px] text-muted-foreground">
              {h.excerpt}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
