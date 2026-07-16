import type { Metadata } from "next";
import Link from "next/link";

import { BoardScreen } from "@/components/kanban/board-screen";

export const metadata: Metadata = { title: "Board" };

/**
 * The canonical Kanban route (Vol 4 §6.3/§11). The project detail page's "board"
 * tab navigates here rather than rendering its own copy, so `?task=` has exactly
 * one home.
 */
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-lg font-semibold">Board</h1>
        <Link
          href={`/projects/${pid}`}
          className="text-sm text-muted-foreground underline"
        >
          Project overview
        </Link>
      </div>
      <BoardScreen projectId={pid} />
    </div>
  );
}
