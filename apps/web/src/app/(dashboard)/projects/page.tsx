import type { Metadata } from "next";

import { ProjectsList } from "@/components/projects/projects-list";

export const metadata: Metadata = { title: "Projects" };

/** Projects list (Vol 4 §6.2) — live on the Phase-57 data layer. */
export default function Page() {
  return <ProjectsList />;
}
