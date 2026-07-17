import type { Metadata } from "next";

import { DeploymentsScreen } from "@/components/deployments/deployments-screen";

export const metadata: Metadata = { title: "Deployments" };

/** Vol 4 §6.9, project-scoped — the only scope the deployments API has. */
export default async function Page({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  return <DeploymentsScreen projectId={pid} />;
}
