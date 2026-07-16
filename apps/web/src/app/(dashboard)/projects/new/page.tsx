import { Suspense } from "react";
import type { Metadata } from "next";

import { IntakeWizard } from "@/components/projects/intake-wizard";

export const metadata: Metadata = { title: "New project" };

/** The chat-style CEO intake wizard (Vol 4 §6.2, TITAN-FRT-023). */
export default function Page() {
  return (
    <Suspense>
      <IntakeWizard />
    </Suspense>
  );
}
