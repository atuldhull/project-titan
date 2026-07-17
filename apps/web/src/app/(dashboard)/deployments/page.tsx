import type { Metadata } from "next";

import { OrgDeployments } from "@/components/deployments/org-deployments";

export const metadata: Metadata = { title: "Deployments" };

/**
 * The org-wide Deployment Dashboard (Vol 4 §6.9).
 *
 * There is no org-wide deployments endpoint — the deploy API is project-scoped
 * only (`GET /projects/{id}/deployments`) — so this fans out over the projects
 * the caller can see, exactly as the Phase-61 agent grid does for runs. That is
 * a deliberate choice over minting an org endpoint: a cross-project list needs
 * per-project visibility filtering to avoid an IDOR, and the project-scoped
 * route already enforces it once per project.
 */
export default function Page() {
  return <OrgDeployments />;
}
