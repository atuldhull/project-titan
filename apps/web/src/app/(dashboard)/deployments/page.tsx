import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Deployments" };

export default function Page() {
  return (
    <StubPage
      title="Deployments"
      description="Org-wide deployment dashboard: environments, pipeline runs, history (Vol 4 §6.9)."
    />
  );
}
