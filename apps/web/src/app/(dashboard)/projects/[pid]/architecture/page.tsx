import type { Metadata } from "next";
import Link from "next/link";

import { ArchitectureScreen } from "@/components/graph/architecture-screen";

export const metadata: Metadata = { title: "Architecture" };

/** The Architect Agent's committed design package (Vol 4 §6.8). */
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-lg font-semibold">Architecture</h1>
        <Link
          href={`/projects/${pid}`}
          className="text-sm text-muted-foreground underline"
        >
          Project overview
        </Link>
      </div>
      <ArchitectureScreen projectId={pid} />
    </div>
  );
}
