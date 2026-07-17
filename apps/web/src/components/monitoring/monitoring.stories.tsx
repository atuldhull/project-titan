import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { Sparkline, StackedSeries } from "./sparkline";
import { stack, type Series } from "@/lib/monitoring/chart";

/**
 * Monitoring's charts (Vol 4 §6.10) in their states. The full screen owns data
 * hooks that Storybook cannot drive, so — as in Phase 62 — the stories render
 * the pure presentational pieces, which is also where the four states live: the
 * charts show "No data in range" from `extentOf === null`, and the loading and
 * error states are the screen's skeleton and banner, unit-tested separately.
 */
const HOUR = 3_600_000;
const BASE = 1_784_000_000_000;

const COST: Series[] = [
  {
    key: "anthropic",
    points: Array.from({ length: 12 }, (_, i) => ({
      x: BASE + i * HOUR,
      y: 0.2 + Math.sin(i / 2) * 0.15,
    })),
  },
  {
    key: "openai",
    points: Array.from({ length: 12 }, (_, i) => ({
      x: BASE + i * HOUR,
      y: 0.1 + (i % 3) * 0.05,
    })),
  },
];

function Panel({ series }: { series: Series[] }) {
  return (
    <div className="max-w-3xl space-y-4 p-4">
      <div className="flex gap-6">
        <div className="text-sky-600 dark:text-sky-400">
          <p className="text-xs text-muted-foreground">LLM spend (24h)</p>
          <Sparkline points={series[0]?.points ?? []} />
        </div>
      </div>
      <StackedSeries
        series={stack(series)}
        label="LLM cost by provider over 24h"
      />
    </div>
  );
}

const meta: Meta<typeof Panel> = {
  title: "Monitoring/Charts",
  component: Panel,
  parameters: { layout: "fullscreen" },
};
export default meta;

type Story = StoryObj<typeof Panel>;

/** Populated: a stacked cost chart over two providers, plus a sparkline. */
export const Populated: Story = { args: { series: COST } };

/** Empty range — §6.10's "No data in range". */
export const Empty: Story = { args: { series: [] } };

/** A single bucket: the line degrades to a visible tick rather than nothing. */
export const SinglePoint: Story = {
  args: { series: [{ key: "anthropic", points: [{ x: BASE, y: 0.5 }] }] },
};
