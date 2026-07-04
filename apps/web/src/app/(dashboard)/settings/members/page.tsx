import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Members" };

export default function Page() {
  return (
    <StubPage
      title="Members"
      description="Organization members and roles (RBAC lands in Phase 9)."
    />
  );
}
