import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Project Overview" };

// The pid comes from the URL — navigational state is URL-encoded and
// restorable on reload (TITAN-FRT-006).
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return (
    <StubPage
      title="Project Overview"
      description="Status, phase progress, recent activity, and cost for this project (Vol 4 §6.2)."
    >
      <p className="mt-3 font-mono text-xs text-muted-foreground">
        project: {pid}
      </p>
    </StubPage>
  );
}
