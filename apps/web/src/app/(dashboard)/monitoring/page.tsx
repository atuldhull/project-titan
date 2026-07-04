import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Monitoring" };

export default function Page() {
  return (
    <StubPage
      title="Monitoring"
      description="Platform and agent metrics: throughput, queue depth, LLM spend (Vol 4 §6.10)."
    />
  );
}
