import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Project Deployments" };

export default function Page() {
  return (
    <StubPage
      title="Project Deployments"
      description="Environments and deploys for this project (Vol 4 §6.9)."
    />
  );
}
