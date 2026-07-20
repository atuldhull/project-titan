"use client";

/**
 * The org-wide deployment list (Vol 4 §6.9's `/deployments`).
 *
 * Fanned out per project rather than served by an org endpoint. The deploy API
 * is project-scoped only, and that is the right place for it to stay: a
 * cross-project list has to filter by per-project visibility or it is an IDOR,
 * and `GET /projects/{id}/deployments` already does exactly that check, once
 * per project, in the one place that owns it. The Phase-61 agent grid fans out
 * over projects for the same reason.
 */
import { useMemo } from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/settings/errors";
import { useProjects } from "@/lib/projects/hooks";
import { useProjectDeployments } from "@/lib/deployments/hooks";

export function OrgDeployments() {
  const projects = useProjects();
  const items = projects.data?.items ?? [];

  if (projects.isLoading) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24" />
      </div>
    );
  }

  return (
    <div className="space-y-4 p-6">
      <header>
        <h1 className="text-xl font-semibold">Deployments</h1>
        <p className="text-xs text-muted-foreground">
          Local Compose stacks across every project you can see.
        </p>
      </header>

      {/* A failed project fetch is NOT "no projects". Until Phase 66 this
          screen had no `isError` branch at all, so `data?.items ?? []` turned a
          403 or a dropped connection into "No projects yet." — a failure to
          find out, rendered as a fact about the deployment. Same defect the
          plugins screen documents guarding against, and the reason
          TITAN-FRT-002 names error and empty as SEPARATE required states. */}
      {projects.isError ? (
        <p
          role="status"
          className="rounded-lg border bg-card p-6 text-sm text-destructive"
        >
          Couldn’t load projects. {errorMessage(projects.error)}{" "}
          <button className="underline" onClick={() => void projects.refetch()}>
            Retry
          </button>
        </p>
      ) : items.length === 0 ? (
        <p className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          No projects yet.
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((p) => (
            <ProjectRow key={p.id} projectId={p.id} name={p.name} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ProjectRow({
  projectId,
  name,
}: {
  readonly projectId: string;
  readonly name: string;
}) {
  const deployments = useProjectDeployments(projectId);
  const rows = useMemo(() => deployments.data ?? [], [deployments.data]);

  return (
    <li className="rounded-lg border bg-card p-3">
      <div className="flex items-center justify-between gap-2">
        <Link
          href={`/projects/${projectId}/deployments`}
          className="text-sm font-medium hover:underline"
        >
          {name}
        </Link>
        {deployments.isLoading ? (
          <Skeleton className="h-4 w-16" />
        ) : deployments.isError ? (
          // One project failing must not blank the others — §6.9's "error
          // inline per environment", applied per project.
          <span className="text-xs text-destructive">unavailable</span>
        ) : (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            {rows.length === 0 ? (
              "no deployments"
            ) : (
              <>
                {rows.filter((d) => d.health === "green").length} healthy
                <span aria-hidden>·</span>
                {rows.length} stack{rows.length === 1 ? "" : "s"}
              </>
            )}
          </span>
        )}
      </div>
      {rows.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {rows.map((d) => (
            <Badge
              key={d.id}
              variant="outline"
              className="font-mono text-[10px]"
            >
              {d.name} · {d.status}
            </Badge>
          ))}
        </div>
      ) : null}
    </li>
  );
}
