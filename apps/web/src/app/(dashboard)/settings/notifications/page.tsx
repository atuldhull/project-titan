import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Notification Settings" };

export default function Page() {
  return (
    <StubPage
      title="Notification Settings"
      description="Delivery preferences for attention items."
    />
  );
}
