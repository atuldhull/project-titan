import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Memory Explorer" };

export default function Page() {
  return (
    <StubPage
      title="Memory Explorer"
      description="Search and inspect the organization brain: memory tiers, provenance, citations (Vol 4 §6.11)."
    />
  );
}
