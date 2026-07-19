import type { Metadata } from "next";

import { PluginsScreen } from "@/components/plugins/plugins-screen";

export const metadata: Metadata = { title: "Plugin Manager" };

export default function Page() {
  return <PluginsScreen />;
}
