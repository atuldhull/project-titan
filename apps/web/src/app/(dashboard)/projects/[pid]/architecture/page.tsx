import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Architecture" };

export default function Page() {
  return (
    <StubPage
      title="Architecture"
      description="Live architecture diagram of the generated system (Vol 4 §6.8)."
    />
  );
}
