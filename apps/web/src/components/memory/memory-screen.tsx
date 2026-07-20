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
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { MemoryDetail } from "@/components/memory/memory-detail";
import { usePrimarySearch } from "@/lib/shortcuts/primary-search";
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

  // §10: `/` focuses "the screen's primary search/filter". Without this the key
  // fell through to the palette, which searches something else entirely.
  const searchRef = useRef<HTMLInputElement>(null);
  usePrimarySearch(searchRef);

  // Below xl, opening a record hides the results list — INCLUDING the button
  // that was just activated, which is where focus is sitting. A focused element
  // that becomes `display: none` does not pass focus to anything; the browser
  // drops it to <body>, so the next Tab restarts from the top of the document
  // and a screen reader announces nothing at all. Silent for a mouse, and a
  // dead end for everyone else.
  //
  // Moving focus to "Back to results" is the fix and it needs no breakpoint
  // test to be correct: at xl that button's wrapper is `xl:hidden`, a
  // display:none element is not focusable, and `.focus()` on one is a no-op —
  // so focus stays put on the result button exactly where both panes are still
  // visible and nothing was taken away.
  const backRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (selected === null) return;
    backRef.current?.focus();
  }, [selected]);

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
    /*
     * `h-full`, not viewport arithmetic. `h-[calc(100vh-4rem)]` assumed the only
     * chrome above this screen was a 4rem bar; the topbar is h-14 (56px) and
     * `<main>` adds `p-4 pb-20` under md and `p-6` at md+, so the subtraction was
     * wrong at BOTH breakpoints — it overshot by 40px at ≥md and pushed a
     * scrollbar onto a screen that manages its own panes. `<main>` is a flex
     * child with a definite height, so `h-full` resolves against it and is
     * correct at every breakpoint without knowing what the shell is made of.
     * `min-h-0` lets the panes below actually shrink and scroll themselves.
     */
    <div className="flex h-full min-h-0 flex-col">
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
            ref={searchRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="semantic search: “auth token refresh”"
            aria-label="Semantic search"
            className="h-11 pl-7 text-xs md:h-8"
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

      {/*
       * §8, "< 768: one panel at a time". At 375px the available width is 343px
       * and this screen used to demand 672px of RIGID chrome (a 224px tier rail
       * plus a 448px detail pane) before the results column got a single pixel —
       * a ~329px overflow of the content region, which §8 forbids outright.
       *
       * The fix is a column below md and a row at md+, plus the panel swap on
       * the aside below. It is all CSS: there is no `matchMedia` anywhere in
       * this app and no jsdom mock for one, so a JS breakpoint here would break
       * the vitest suite in exchange for something the cascade already knows.
       */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <nav
          /*
           * Vertical rail at md+, horizontal chip strip below — deliberately NOT
           * folded into the app hamburger: these are FILTERS over this screen,
           * not app navigation, and burying a filter in the global nav is how a
           * user ends up believing the memory store is empty.
           *
           * `gap-1` rather than `space-y-1` at both breakpoints: `space-y-*` is
           * a vertical-margin utility and would put its gaps in the wrong axis
           * once the strip is a row.
           *
           * `md:overflow-y-auto` with no `overflow-x-visible` counterpart: CSS
           * computes `visible` back to `auto` the moment the other axis is
           * non-visible, so the reset would be a class that reads as meaningful
           * and does nothing.
           */
          className="flex shrink-0 gap-1 overflow-x-auto border-b p-2 md:w-56 md:flex-col md:overflow-y-auto md:border-r md:border-b-0"
          aria-label="Memory tiers"
        >
          {facets.isLoading ? (
            <Skeleton className="h-11 w-full md:h-24" />
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
                  // `shrink-0` + `whitespace-nowrap`: in the strip these are
                  // flex ROW children, and without it every chip compresses to
                  // fit rather than scrolling. `min-h-11` is §8's 44px touch
                  // minimum, dropped at md+ where the rail is pointer-driven.
                  "flex min-h-11 shrink-0 items-center justify-between gap-2 rounded px-2 text-left text-xs whitespace-nowrap",
                  "md:min-h-0 md:w-full md:py-1",
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
          className={cn(
            "min-w-0 flex-1 overflow-auto p-2",
            // One panel at a time: below xl, opening a record REPLACES the
            // results list rather than squeezing alongside it. At xl both are
            // inline, which is §8's "two-panel screens keep both panels" with
            // the inspector as the third thing that has to give way first.
            selected !== null && "hidden xl:block",
          )}
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
          className={cn(
            "min-w-0 overflow-auto",
            // Inline third pane only at xl. §8 puts Memory's inspector behind
            // an overlay below 1280, and 448px of fixed detail beside a 224px
            // rail leaves the results column unusable long before 768.
            "xl:block xl:w-[28rem] xl:shrink-0 xl:border-l",
            // ONE mount, whose visibility is all that changes: rendering a
            // second copy in a Sheet would double every detail query and —
            // worse — a Radix Sheet keeps its focus trap and body-scroll lock
            // whatever CSS says about it, so at xl the inline pane would be
            // visible while a trap held focus somewhere else entirely.
            //
            // `flex-1` is what makes this pane FILL the space the hidden
            // results list vacated below xl — in the column layout under md and
            // in the row layout between md and xl alike. It has to be undone at
            // xl, though, and `xl:shrink-0` is not enough to do it: Tailwind's
            // `flex-1` compiles to the `flex: 1` SHORTHAND, which also sets
            // `flex-basis: 0%`, and a 0% basis with `flex-grow: 1` beats
            // `width: 28rem` outright. The results <section> beside it is also
            // `flex-1`, so the two would have split the row 50/50 and the fixed
            // 448px inspector would have silently stopped being fixed. `flex:
            // none` (0 0 auto) restores `width` as the used width.
            selected === null ? "hidden xl:block" : "flex-1 xl:flex-none",
          )}
          aria-label="Detail"
        >
          {/*
            The way back. Below xl the results list is hidden while a record is
            open, so without this the only exit is the browser's Back button —
            which does not undo a selection, because the selection is state, not
            a route. Hidden at xl, where both panes are visible and there is
            nothing to go back FROM.
          */}
          {selected !== null ? (
            <div className="border-b p-2 xl:hidden">
              {/* `min-h-11` with no `md:` escape hatch, unlike the other 44px
                  fixes in this sweep: this control is `xl:hidden`, so the only
                  viewports it EXISTS at are the ones §8 calls touch. Shedding
                  the touch target at md would shrink it across 768–1279 —
                  tablet range, thumb-driven, and the same reasoning
                  `settings-nav.tsx` uses to hold its own minimum until lg. */}
              <Button
                ref={backRef}
                size="sm"
                variant="ghost"
                className="min-h-11 gap-1 text-xs"
                onClick={() => setSelected(null)}
              >
                <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                Back to results
              </Button>
            </div>
          ) : null}
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
