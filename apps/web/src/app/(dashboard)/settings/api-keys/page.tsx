import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "API Keys" };

export default function Page() {
  return (
    <StubPage
      title="API Keys"
      description="Personal access tokens for the TITAN API."
    />
  );
}
