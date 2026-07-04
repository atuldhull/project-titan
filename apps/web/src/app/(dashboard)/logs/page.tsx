import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Logs" };

// ?project= filter is URL state per TITAN-FRT-006 — already honored here so
// deep links stay stable when the full viewer lands.
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ project?: string }>;
}) {
  const { project } = await searchParams;
  return (
    <StubPage
      title="Logs"
      description="Org-wide structured log viewer with level, agent, and project filters (Vol 4 §6.7)."
    >
      {project ? (
        <p className="mt-3 font-mono text-xs text-muted-foreground">
          filter: project={project}
        </p>
      ) : null}
    </StubPage>
  );
}
