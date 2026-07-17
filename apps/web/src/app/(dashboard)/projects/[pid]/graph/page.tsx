import type { Metadata } from "next";
import Link from "next/link";

import { TaskGraphScreen } from "@/components/graph/task-graph-screen";

export const metadata: Metadata = { title: "Task Graph" };

/** The task dependency DAG (Vol 4 §6.4). */
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-lg font-semibold">Task graph</h1>
        <Link
          href={`/projects/${pid}`}
          className="text-sm text-muted-foreground underline"
        >
          Project overview
        </Link>
      </div>
      <TaskGraphScreen projectId={pid} />
    </div>
  );
}
