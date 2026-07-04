import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Board" };

export default function Page() {
  return (
    <StubPage
      title="Board"
      description="Kanban board: task columns with drag-and-drop and swimlanes (Vol 4 §6.3)."
    />
  );
}
