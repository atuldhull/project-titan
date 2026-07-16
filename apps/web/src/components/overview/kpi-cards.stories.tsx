import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { CostKpiCard, KpiCard } from "./kpi-cards";

/**
 * The §6.1 KPI cards in all four states (TITAN-FRT-002). Each card owns its own
 * state so a failing panel degrades alone — the Error story is what that looks
 * like next to healthy neighbours.
 */
const meta: Meta<typeof KpiCard> = {
  title: "Overview/KpiCard",
  component: KpiCard,
};
export default meta;

type Story = StoryObj<typeof KpiCard>;

export const Populated: Story = {
  args: { title: "Tasks", value: "37", sub: "5 blocked", href: "/projects" },
};

export const Loading: Story = {
  args: { title: "Tasks", state: "loading" },
};

export const Empty: Story = {
  name: "Empty (zero work)",
  args: { title: "Tasks", value: "0", sub: "none blocked" },
};

export const Error: Story = {
  args: { title: "Tasks", state: "error" },
};

/**
 * The Cost card has no backend at all — its only honest state. Kept as a story
 * so the "not available" treatment is reviewable rather than mistaken for a bug.
 */
export const CostUnavailable: StoryObj<typeof CostKpiCard> = {
  render: () => <CostKpiCard />,
};
