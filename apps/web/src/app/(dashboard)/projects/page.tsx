import type { Metadata } from "next";

import { CeoIntakeDemo } from "@/components/projects/ceo-intake-demo";

export const metadata: Metadata = { title: "Projects" };

// Phase 25 walking skeleton: the full filterable project list (Vol 4 §6.2) lands
// with the dashboard build-out (M6). For now this surface hosts the throwaway CEO
// intake demo that proves the brief → streamed stories spine in the browser.
export default function Page() {
  return <CeoIntakeDemo />;
}
