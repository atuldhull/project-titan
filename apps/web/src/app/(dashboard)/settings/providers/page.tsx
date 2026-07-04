import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "LLM Providers" };

export default function Page() {
  return (
    <StubPage
      title="LLM Providers"
      description="Provider keys and model routing (config-only switching, ADR-007)."
    />
  );
}
