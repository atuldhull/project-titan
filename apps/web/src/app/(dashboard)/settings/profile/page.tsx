import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Profile" };

export default function Page() {
  return <StubPage title="Profile" description="Your account details." />;
}
