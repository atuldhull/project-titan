import type { Metadata } from "next";

import { MemoryScreen } from "@/components/memory/memory-screen";

export const metadata: Metadata = { title: "Memory" };

/** The Memory Explorer (Vol 4 §6.11, TITAN-FRT-034). */
export default function Page() {
  return <MemoryScreen />;
}
