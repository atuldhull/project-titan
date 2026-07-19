import type { Metadata } from "next";

import { BudgetsScreen } from "@/components/settings/budgets-screen";

export const metadata: Metadata = { title: "Budgets" };

/** Settings → Budgets (Vol 4 §6.13, the §8.4 spend caps). */
export default function Page() {
  return <BudgetsScreen />;
}
