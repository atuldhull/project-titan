import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Budgets" };

export default function Page() {
  return (
    <StubPage
      title="Budgets"
      description="Per-project LLM budget caps (wired in Phase 17)."
    />
  );
}
