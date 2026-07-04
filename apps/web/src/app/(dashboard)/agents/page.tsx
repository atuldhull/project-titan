import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Agents" };

export default function Page() {
  return (
    <StubPage
      title="Agents"
      description="The 16-agent status grid: state, current task, model binding, and KPIs (Vol 4 §6.5)."
    />
  );
}
