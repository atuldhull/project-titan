"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Plus, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { agentsByProject, boardStats } from "@/lib/projects/derive";
import {
  useAgents,
  useProjectBoards,
  useProjects,
  type ProjectSort,
} from "@/lib/projects/hooks";

/**
 * Projects list (Vol 4 §6.2): filterable, sortable rows showing status,
 * progress, agents, CI and last activity.
 *
 * Contract notes that shape this:
 *  - server-side sort is limited to `^-?created_at$`, so "newest/oldest" are the
 *    only true sorts; a name sort would silently only reorder the fetched page,
 *    so it is done client-side over the full (<=200) list and labelled as such.
 *  - `ProjectOut` has no `updated_at`, so "last activity" can only honestly be
 *    the creation time.
 *  - progress/agents are derived (see derive.ts); CI has no source -> "–".
 */
export function ProjectsList() {
  const [status, setStatus] = useState<string>("");
  const [sort, setSort] = useState<ProjectSort>("-created_at");
  const [search, setSearch] = useState("");

  const query = useProjects({ status: status || undefined, sort });
  const agentsQuery = useAgents();

  const projects = useMemo(() => query.data?.items ?? [], [query.data]);
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return needle
      ? projects.filter(
          (p) =>
            p.display_name.toLowerCase().includes(needle) ||
            p.name.toLowerCase().includes(needle),
        )
      : projects;
  }, [projects, search]);

  const ids = useMemo(() => filtered.map((p) => p.id), [filtered]);
  const boards = useProjectBoards(ids);
  const agentsByPid = useMemo(
    () => agentsByProject(agentsQuery.data?.items ?? []),
    [agentsQuery.data],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Projects</h1>
        <Button asChild size="sm" className="ml-auto">
          <Link href="/projects/new">
            <Plus aria-hidden />
            New project
          </Link>
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search
            aria-hidden
            className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter by name…"
            aria-label="Filter projects by name"
            className="w-56 pl-7"
          />
        </div>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by status"
          className="h-9 rounded-md border bg-background px-2 text-sm"
        >
          {/* Only statuses the backend actually writes: `intake` is the column
              default, and `archived`/`released` are what set_project_status
              sets. Offering "Active"/"Paused" would be a filter that can never
              match anything. */}
          <option value="">All statuses</option>
          <option value="intake">Intake</option>
          <option value="released">Released</option>
          <option value="archived">Archived</option>
        </select>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as ProjectSort)}
          aria-label="Sort projects"
          className="h-9 rounded-md border bg-background px-2 text-sm"
        >
          <option value="-created_at">Newest first</option>
          <option value="created_at">Oldest first</option>
        </select>
      </div>

      {query.isLoading ? (
        <ul className="divide-y rounded-lg border bg-card">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex items-center gap-4 p-3">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-1.5 w-32" />
              <Skeleton className="ml-auto h-4 w-24" />
            </li>
          ))}
        </ul>
      ) : query.isError ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-destructive">
          Couldn’t load projects.{" "}
          <button
            type="button"
            className="underline underline-offset-4"
            onClick={() => void query.refetch()}
          >
            Retry
          </button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed p-8 text-center">
          <p className="text-sm text-muted-foreground">
            {projects.length === 0
              ? "No projects yet — describe one and the team starts planning."
              : "No projects match this filter."}
          </p>
          {projects.length === 0 ? (
            <Button asChild size="sm">
              <Link href="/projects/new">Start a project</Link>
            </Button>
          ) : null}
        </div>
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {filtered.map((project, index) => {
            const stats = boardStats(boards[index]?.data);
            const agents = agentsByPid.get(project.id);
            return (
              <li key={project.id}>
                <Link
                  href={`/projects/${project.id}`}
                  className="flex flex-wrap items-center gap-x-4 gap-y-2 p-3 hover:bg-accent/40"
                >
                  <span className="min-w-40 flex-1 truncate font-medium">
                    {project.display_name}
                  </span>
                  <Badge variant="outline" className="capitalize">
                    {project.status}
                  </Badge>
                  <span className="flex w-28 items-center gap-2">
                    <Progress value={stats.progressPct} className="w-16" />
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {stats.progressPct === null
                        ? "—"
                        : `${stats.progressPct}%`}
                    </span>
                  </span>
                  <span className="w-20 text-xs text-muted-foreground">
                    {agents?.total ?? 0} agents
                  </span>
                  <span
                    className="w-12 text-xs text-muted-foreground"
                    title="No CI signal is exposed by the backend yet"
                  >
                    CI –
                  </span>
                  {stats.blocked > 0 ? (
                    <span className="text-xs font-medium text-warning">
                      {stats.blocked} blocked
                    </span>
                  ) : null}
                  <span
                    className="w-28 text-right text-xs text-muted-foreground"
                    title="ProjectOut exposes no updated_at — this is the creation time"
                  >
                    {new Date(project.created_at).toLocaleDateString()}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
