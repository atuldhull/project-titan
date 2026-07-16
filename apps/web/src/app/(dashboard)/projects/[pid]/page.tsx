import { Suspense } from "react";
import type { Metadata } from "next";

import { ProjectDetail } from "@/components/projects/project-detail";

export const metadata: Metadata = { title: "Project" };

/**
 * Project detail (Vol 4 §6.2). The pid and the active tab live in the URL, so
 * navigational state is restorable on reload (TITAN-FRT-006).
 */
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return (
    <Suspense>
      <ProjectDetail projectId={pid} />
    </Suspense>
  );
}
