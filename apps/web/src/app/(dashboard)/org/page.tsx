import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Organization Overview" };

export default function Page() {
  return (
    <StubPage
      title="Organization Overview"
      description="Company-wide health: active projects, agent activity, spend, and attention items (Vol 4 §6.1)."
    />
  );
}
