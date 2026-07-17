import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import type { LogLine } from "@titan/api-client";

import { GapMarker } from "./gap-marker";
import { LogFilterBar, FilterSummary } from "./log-filter-bar";
import { LogRow } from "./log-row";
import {
  EMPTY_FILTERS,
  compileRegex,
  type LogFilters,
} from "@/lib/logs/filters";
import { openGap, fillGap, failGap, loadingGap } from "@/lib/logs/gaps";

/**
 * The Logs Viewer's four states (§6.7), composed from the real row, marker and
 * filter-bar components.
 *
 * The stories render the PIECES rather than mounting `LogsScreen` itself: the
 * screen owns a virtualizer, and a virtualized list has no height in a story
 * frame any more than it does in jsdom — react-virtual bails out at
 * `outerSize === 0` and shows nothing. Rendering rows directly is what actually
 * exercises the visual states these stories exist to review, including the
 * TITAN-FRT-044 greyscale check (every level pairs its colour with a distinct
 * icon shape and a text label).
 */
const BASE = BigInt("1700000000000000000");

function line(
  i: number,
  level: string,
  text: string,
  over: Partial<LogLine> = {},
): LogLine {
  const ts = String(BASE + BigInt(i) * BigInt(1_000_000_000));
  return {
    id: `l${i}`,
    ts_ns: ts,
    line: text,
    labels: { namespace: "titan", service: i % 2 ? "worker" : "api", level },
    level,
    event: text,
    module: "titan.runs.service",
    request_id: `req-${i}`,
    trace_id: null,
    span_id: null,
    project_id: null,
    task_id: null,
    agent_id: null,
    fields: {},
    ...over,
  } as LogLine;
}

const LINES: LogLine[] = [
  line(
    0,
    "info",
    '{"event":"starting payment module","task":"T-231","level":"info"}',
  ),
  line(
    1,
    "warning",
    '{"event":"flaky test retry 2/3 e2e/checkout","level":"warning"}',
  ),
  line(
    2,
    "error",
    '{"event":"dep scan: lodash@4.17.20 CVE-2021-23337","level":"error"}',
    {
      task_id: "22222222-2222-7222-8222-222222222222",
      project_id: "11111111-1111-7111-8111-111111111111",
    },
  ),
  line(3, "info", '{"event":"staging rollout 3/4 pods ready","level":"info"}'),
  line(4, "debug", '{"event":"cache hit ratio 0.94","level":"debug"}'),
  line(
    5,
    "critical",
    '{"event":"ledger reconciliation mismatch","level":"critical"}',
  ),
  // A container that does not log JSON — roughly half of what promtail ships.
  {
    ...line(6, "info", "LOG:  database system is ready to accept connections"),
    labels: { namespace: "titan", service: "postgres" },
    level: null,
    event: null,
  } as LogLine,
];

function Rows({
  filters,
  expanded,
}: {
  filters: LogFilters;
  expanded?: boolean;
}) {
  const compiled = compileRegex(filters.regex);
  return (
    <div className="rounded-md border">
      {LINES.map((l) => (
        <LogRow
          key={l.id}
          line={l}
          filters={filters}
          regex={compiled.ok ? compiled.regex : null}
          wrap={false}
          expanded={Boolean(expanded) && l.level === "error"}
          onToggle={() => {}}
          onOpenContext={() => {}}
        />
      ))}
    </div>
  );
}

function Screen({
  state,
  filters = EMPTY_FILTERS,
}: {
  state: "loaded" | "empty" | "error" | "gaps";
  filters?: LogFilters;
}) {
  return (
    <div className="flex h-[420px] w-full flex-col">
      <LogFilterBar
        filters={filters}
        onChange={() => {}}
        tailing={state === "loaded"}
        onToggleTail={() => {}}
        wrap={false}
        onToggleWrap={() => {}}
        onExport={() => {}}
        shown={state === "empty" ? 0 : LINES.length}
        regexError={null}
      />
      {state === "error" ? (
        <div className="p-4">
          <p
            role="status"
            className="rounded-md border bg-muted/50 px-2 py-1 text-xs text-muted-foreground"
          >
            Live tail interrupted (503) — showing the lines already loaded.
          </p>
          <div className="mt-2">
            <Rows filters={filters} />
          </div>
        </div>
      ) : state === "empty" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
          <p>No log lines match filters</p>
          <FilterSummary filters={filters} />
        </div>
      ) : state === "gaps" ? (
        <div className="rounded-md">
          <Rows filters={filters} />
          <GapMarker
            gap={openGap(String(BASE), String(BASE + BigInt("134000000000")))}
            onLoad={() => {}}
          />
          <GapMarker
            gap={loadingGap(
              openGap(String(BASE), String(BASE + BigInt("2500000000"))),
            )}
            onLoad={() => {}}
          />
          <GapMarker
            gap={fillGap(
              openGap(String(BASE), String(BASE + BigInt("134000000000"))),
              500,
              "cursor-1",
            )}
            onLoad={() => {}}
          />
          <GapMarker
            gap={failGap(
              openGap(String(BASE), String(BASE + BigInt("9000000000"))),
              "HTTP 503",
            )}
            onLoad={() => {}}
          />
        </div>
      ) : (
        <Rows filters={filters} expanded />
      )}
    </div>
  );
}

const meta: Meta<typeof Screen> = {
  title: "Logs/LogsScreen",
  component: Screen,
  parameters: { layout: "fullscreen" },
};
export default meta;

type Story = StoryObj<typeof Screen>;

/** Loaded and tailing: every level, plus a plain-text postgres line. */
export const Loaded: Story = { args: { state: "loaded" } };

/** A regex filter with match highlighting (§6.7). */
export const Filtered: Story = {
  args: {
    state: "loaded",
    filters: { ...EMPTY_FILTERS, regex: "retry|CVE-\\d+", levels: [] },
  },
};

/** Empty: §6.7's "No log lines match filters" with a one-click reset. */
export const Empty: Story = {
  args: {
    state: "empty",
    filters: {
      ...EMPTY_FILTERS,
      levels: ["error"],
      search: "nothing-matches-this",
    },
  },
};

/** Error: a banner, and the buffer is retained (§6.7's error state). */
export const StreamError: Story = { args: { state: "error" } };

/** Every gap-marker state: idle, loading, partially filled, and failed. */
export const Gaps: Story = { args: { state: "gaps" } };
