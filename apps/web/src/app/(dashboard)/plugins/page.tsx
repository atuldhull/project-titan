import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Plugin Manager" };

export default function Page() {
  return (
    <StubPage
      title="Plugin Manager"
      description="Installed plugins, MCP servers, and tool registry state (Vol 4 §6.12)."
    />
  );
}
