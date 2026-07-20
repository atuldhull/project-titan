"use client";

/**
 * Monitoring (Vol 4 §6.10, TITAN-FRT-033).
 *
 * **What is real here, and what §6.10 asks for that has no backend.** The scout
 * for this phase found the monitoring backend was not partial — it was absent —
 * so the honest split matters more than usual:
 *
 * - **Real**: everything about LLM cost and tokens. The Phase-17 ledger has
 *   written provider, model, agent, project, tokens and `cost_usd` for every
 *   gateway call since it landed, and Phase 63 is the first thing to read it
 *   back. Spend tiles, the group-by chart and the time series are all it.
 * - **Not real — no metrics API exists.** §6.10's other three tiles (API p95,
 *   error rate, queue depth) live in Prometheus text on `/metrics`, which is
 *   `include_in_schema=False`, absent from the contract and the typed client,
 *   and scraped by a Prometheus that is Compose-profile-gated and unreachable
 *   from the api container. There is no range/query API and no proxy. Rather
 *   than draw three tiles from nothing, the screen says where they live and
 *   links to Grafana, which §6.10 already names as the power-user path.
 * - **Not real — no alerts.** There is no alerts table, no Alertmanager bridge
 *   and no Ack endpoint; `[Ack]` would be a button that does nothing.
 * - **Not real — `metric.sample`.** Zero producers. The tiles poll.
 */
import { Fragment, useMemo, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Sparkline, StackedSeries } from "@/components/monitoring/sparkline";
import { useUsage } from "@/lib/monitoring/hooks";
import {
  formatTokens,
  formatUsd,
  GROUP_BYS,
  RANGES,
  spendPerHour,
  toSeries,
  type GroupBy,
  type RangeKey,
} from "@/lib/monitoring/usage";
import { ApiError } from "@titan/api-client";

const RANGE_KEYS = Object.keys(RANGES) as RangeKey[];

export function MonitoringScreen() {
  const [range, setRange] = useState<RangeKey>("24h");
  const [groupBy, setGroupBy] = useState<GroupBy>("provider");

  const usage = useUsage(range, groupBy);
  // Its own memo: `usage.data?.series ?? []` mints a NEW array on every render
  // while the query is loading or errored, which would make every memo below it
  // recompute forever.
  const points = useMemo(() => usage.data?.series ?? [], [usage.data]);

  // BOTH charts stack, and `StackedSeries` now does the stacking (and the
  // x-axis alignment that stops a bucket one group is missing from drawing a
  // line straight through zero) ITSELF, from these raw per-group series. That
  // moved in Phase 65 so the chart and its TITAN-FRT-047 table are two views of
  // one array: a table fed the cumulative data would have reported every group
  // as the sum of itself and everything under it.
  const costSeries = useMemo(() => toSeries(points, "cost"), [points]);
  const tokenSeries = useMemo(() => toSeries(points, "tokens"), [points]);
  // The spend sparkline is the whole range's cost per bucket, so it sums the
  // groups rather than picking one.
  const spendSpark = useMemo(() => {
    const byX = new Map<number, number>();
    for (const s of costSeries) {
      for (const p of s.points) byX.set(p.x, (byX.get(p.x) ?? 0) + p.y);
    }
    return Array.from(byX, ([x, y]) => ({ x, y })).sort((a, b) => a.x - b.x);
  }, [costSeries]);

  const total = usage.data?.total;
  // null, not 0, when there is no data: a failed or empty request must show "—"
  // like the other tiles, not a confident "$0.00" beside them — which reads as
  // "we spent nothing" rather than "we don't know".
  const totalUsd = total ? Number(total.cost_usd) : null;
  const perHour =
    totalUsd === null ? null : spendPerHour(totalUsd, RANGES[range].ms);

  const groups = usage.data?.groups ?? [];
  /**
   * ONE definition of what a group row shows, rendered twice (table at md+,
   * cards below) rather than written twice.
   *
   * The group-key column is deliberately NOT in here: it is the row's heading
   * in both layouts — a `<th scope="row">` in the table and the card's title —
   * so it is the one field whose two renderings genuinely differ.
   */
  const groupColumns: readonly {
    key: string;
    header: string;
    cell: (group: (typeof groups)[number]) => ReactNode;
  }[] = [
    {
      key: "cost",
      header: "cost",
      // The string, not a re-formatted float: this column is the ledger's own
      // number, and it is asserted to the micro-dollar against it.
      cell: (group) => `$${group.cost_usd}`,
    },
    {
      key: "tokens",
      header: "tokens in/out",
      cell: (group) =>
        `${formatTokens(group.input_tokens)} / ${formatTokens(group.output_tokens)}`,
    },
    {
      key: "calls",
      header: "calls",
      cell: (group) => group.calls.toLocaleString(),
    },
  ];

  if (usage.isLoading) {
    return (
      <div className="space-y-4 p-6">
        <Skeleton className="h-8 w-64" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
        <Skeleton className="h-56 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Monitoring</h1>
          <p className="text-xs text-muted-foreground">
            LLM cost and token usage from the gateway ledger.
          </p>
        </div>
        <div className="flex items-center gap-1">
          <span className="mr-1 text-xs text-muted-foreground">Range</span>
          {RANGE_KEYS.map((key) => (
            <Button
              key={key}
              size="sm"
              variant={range === key ? "secondary" : "ghost"}
              aria-pressed={range === key}
              className="h-7 px-2 text-xs"
              onClick={() => setRange(key)}
            >
              {key}
            </Button>
          ))}
        </div>
      </header>

      {usage.isError ? (
        <p
          className="rounded-md border bg-card p-4 text-sm text-destructive"
          role="status"
        >
          Couldn’t load usage
          {usage.error instanceof ApiError
            ? ` (HTTP ${usage.error.status})`
            : ""}
          .{" "}
          <button className="underline" onClick={() => usage.refetch()}>
            Retry
          </button>
        </p>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label={`LLM spend (${range})`}
          value={totalUsd === null ? "—" : formatUsd(totalUsd)}
          spark={spendSpark}
        />
        <Tile
          label="LLM spend/hr"
          value={perHour === null ? "—" : formatUsd(perHour)}
          hint="Projected from the selected range — there is no per-hour counter."
        />
        <Tile
          label="Tokens in / out"
          value={
            total
              ? `${formatTokens(total.input_tokens)} / ${formatTokens(total.output_tokens)}`
              : "—"
          }
        />
        <Tile
          label="Gateway calls"
          value={total ? total.calls.toLocaleString() : "—"}
        />
      </section>

      <section className="space-y-3 rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-medium">Cost over time</h2>
          <div className="flex items-center gap-1">
            <span className="mr-1 text-xs text-muted-foreground">Group by</span>
            {GROUP_BYS.map((key) => (
              <Button
                key={key}
                size="sm"
                variant={groupBy === key ? "secondary" : "ghost"}
                aria-pressed={groupBy === key}
                className="h-6 px-2 text-[11px]"
                onClick={() => setGroupBy(key)}
              >
                {key}
              </Button>
            ))}
          </div>
        </div>
        {usage.data?.truncated ? (
          <p className="text-[11px] text-warning" role="status">
            Showing the dearest groups only — the chart is trimmed, but the
            totals above are not.
          </p>
        ) : null}
        <StackedSeries
          series={costSeries}
          label={`LLM cost by ${groupBy} over ${range}`}
          format={formatUsd}
        />
      </section>

      <section className="space-y-3 rounded-lg border bg-card p-4">
        <h2 className="text-sm font-medium">Tokens over time</h2>
        <StackedSeries
          series={tokenSeries}
          height={160}
          label={`Token usage by ${groupBy} over ${range}`}
          format={formatTokens}
        />
      </section>

      <section className="space-y-3 rounded-lg border bg-card p-4">
        <h2 className="text-sm font-medium">Cost by {groupBy}</h2>
        {groups.length === 0 ? (
          <p className="text-xs text-muted-foreground">No data in range.</p>
        ) : (
          <>
            {/*
             * §8: "tables → card lists" below 768. This table is a raw <table>
             * rather than `components/ui/table`, so it had no scroll port of its
             * own either — four numeric columns at 375px pushed the whole
             * CONTENT REGION sideways, which §8 forbids outright ("no horizontal
             * body scroll ever"). It gets both: its own `overflow-x-auto` at
             * md+, and cards below.
             */}
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 font-medium">
                      {groupBy}
                    </th>
                    {groupColumns.map((column) => (
                      <th
                        key={column.key}
                        scope="col"
                        className="py-1 text-right font-medium whitespace-nowrap"
                      >
                        {column.header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {groups.map((group) => (
                    <tr key={group.key ?? "(none)"} className="border-t">
                      <th
                        scope="row"
                        className="py-1 text-left font-normal break-all"
                      >
                        {groupLabel(group.key)}
                      </th>
                      {groupColumns.map((column) => (
                        <td
                          key={column.key}
                          className="py-1 text-right tabular-nums whitespace-nowrap"
                        >
                          {column.cell(group)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <ul className="space-y-2 md:hidden">
              {groups.map((group) => (
                <li
                  key={group.key ?? "(none)"}
                  className="rounded-md border p-2"
                >
                  {/* `break-all`: a group key is server-driven and can be a
                      model id with no break opportunity in it at all
                      (`claude-opus-4-20250514`), which at 343px is exactly how
                      a card list ends up overflowing anyway. */}
                  <p className="font-mono text-xs font-medium break-all">
                    {groupLabel(group.key)}
                  </p>
                  <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
                    {groupColumns.map((column) => (
                      <Fragment key={column.key}>
                        <dt className="text-muted-foreground">
                          {column.header}
                        </dt>
                        <dd className="font-mono tabular-nums">
                          {column.cell(group)}
                        </dd>
                      </Fragment>
                    ))}
                  </dl>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="space-y-2 rounded-lg border border-dashed p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Golden signals and alerts are not on this screen yet.
        </p>
        <p>
          §6.10 also specifies API p95, error rate, queue depth and an alert
          list with Ack. Those series exist — the API exports{" "}
          <code className="font-mono">titan_http_request_duration_seconds</code>{" "}
          and <code className="font-mono">titan_stream_depth</code> among others
          — but only as Prometheus text on{" "}
          <code className="font-mono">/metrics</code>. There is no range-query
          API over them and no alerts model, so the honest place to read them
          today is Grafana.
        </p>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button asChild size="sm" variant="outline">
            {/* §6.10: deep-link out, embed nothing — the dashboard stays
                offline-consistent. Grafana is Compose-profile-gated, so this
                is a plain link that 404s honestly when it is not running. */}
            <a
              href="http://localhost:3001"
              target="_blank"
              rel="noreferrer noopener"
            >
              Open in Grafana
            </a>
          </Button>
          <Badge variant="secondary" className="font-mono text-[10px]">
            docker compose --profile obs up
          </Badge>
        </div>
      </section>
    </div>
  );
}

/** A null group key is real and common (unbound project/agent) — a group, not
 *  a gap, so it is named rather than left blank. */
function groupLabel(key: string | null | undefined): string {
  return key ?? "(none)";
}

function Tile({
  label,
  value,
  spark,
  hint,
}: {
  readonly label: string;
  readonly value: string;
  readonly spark?: readonly { x: number; y: number }[];
  readonly hint?: string;
}) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="mt-1 flex items-end justify-between gap-2">
        <span className="text-xl font-semibold tabular-nums">{value}</span>
        {spark && spark.length > 0 ? (
          <span className="text-primary">
            <Sparkline points={spark} />
          </span>
        ) : null}
      </div>
      {hint ? (
        <p className="mt-1 text-[10px] text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}
