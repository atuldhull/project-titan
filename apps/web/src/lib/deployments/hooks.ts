"use client";

import { useQuery } from "@tanstack/react-query";

import { unwrap, type DeploymentOut } from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

/**
 * A project's deployments.
 *
 * Polled, not pushed. §6.9 specifies `deployment.status_changed` and
 * `deployment.step` animating the stage chips; neither exists — zero producers
 * anywhere, and `titan.deploy` does not import the event system at all. The one
 * deploy-ish event, `deploy.incident`, is written by a gate whose only callers
 * are tests, and would be silently dropped by the fanout anyway (no `_HANDLERS`
 * entry → logged at debug → acked). So there is nothing to subscribe to.
 */
const REFRESH_MS = 10_000;

export function useProjectDeployments(projectId: string) {
  return useQuery({
    queryKey: queryKeys.projectDeployments(projectId),
    queryFn: async (): Promise<DeploymentOut[]> =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/deployments", {
          params: { path: { project_id: projectId } },
        }),
      ),
    refetchInterval: REFRESH_MS,
    retry: false,
  });
}
