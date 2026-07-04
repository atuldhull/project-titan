import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Task Graph" };

export default function Page() {
  return (
    <StubPage
      title="Task Graph"
      description="Dependency graph of tasks with critical path (Vol 4 §6.4)."
    />
  );
}
