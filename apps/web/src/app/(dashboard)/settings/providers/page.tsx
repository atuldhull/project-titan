import type { Metadata } from "next";

import { ProvidersScreen } from "@/components/settings/providers-screen";

export const metadata: Metadata = { title: "LLM Providers" };

export default function Page() {
  return <ProvidersScreen />;
}
