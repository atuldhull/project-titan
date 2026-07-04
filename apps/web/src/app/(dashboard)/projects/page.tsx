import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Projects" };

export default function Page() {
  return (
    <StubPage
      title="Projects"
      description="Every project with status, phase, progress, and cost (Vol 4 §6.2)."
    />
  );
}
