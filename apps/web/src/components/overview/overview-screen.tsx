"use client";

import { useMemo } from "react";
import Link from "next/link";
import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  agentsByProject,
  boardStats,
  countWorking,
} from "@/lib/projects/derive";
import { useAgents, useProjectBoards, useProjects } from "@/lib/projects/hooks";
import { useTopic } from "@/lib/ws/use-topic";

import { ActivityFeed } from "./activity-feed";
import { AttentionApprovals } from "./attention-approvals";
import { CostKpiCard, KpiCard } from "./kpi-cards";
import { ProjectCard, ProjectCardSkeleton } from "./project-card";

/**
 * Projects in these states are NOT active. Mirrors the backend's own definition
 * (projects/repository.py: `WHERE status NOT IN ('archived', 'released')`) —
 * inventing extra states here would both miss `released` and carry states no
 * writer ever sets.
 */
const TERMINAL_PROJECT_STATES: ReadonlySet<string> = new Set([
  "archived",
  "released",
]);

/**
 * Organization Overview (Vol 4 §6.1).
 *
 * Realtime: subscribes to the always-on agent-status channel and the org
 * activity feed. KPI values are derived (see lib/projects/derive.ts) and each
 * panel degrades independently — a failing board doesn't blank the row.
 */
export function OverviewScreen() {
  useTopic("agentsStatus");
  useTopic("orgActivity");

  const projectsQuery = useProjects();
  const agentsQuery = useAgents();

  const projects = useMemo(
    () => projectsQuery.data?.items ?? [],
    [projectsQuery.data],
  );
  const active = useMemo(
    () => projects.filter((p) => !TERMINAL_PROJECT_STATES.has(p.status)),
    [projects],
  );
  const activeIds = useMemo(() => active.map((p) => p.id), [active]);

  const boards = useProjectBoards(activeIds);
  const agents = useMemo(
    () => agentsQuery.data?.items ?? [],
    [agentsQuery.data],
  );
  // /agents has no project filter, so one bounded list is grouped client-side.
  const agentsByPid = useMemo(() => agentsByProject(agents), [agents]);

  const taskTotals = useMemo(() => {
    let total = 0;
    let blocked = 0;
    let failed = 0;
    for (const board of boards) {
      if (board.isError) {
        failed += 1;
        continue;
      }
      if (!board.data) {
        continue;
      }
      const stats = boardStats(board.data);
      total += stats.total;
      blocked += stats.blocked;
    }
    // `failed` matters: a partial aggregate must not be presented as the truth.
    // Zero active projects is a legitimate ZERO, not an error — the old check
    // ("no board resolved") reported an error for an org with no active work.
    return { total, blocked, failed };
  }, [boards]);

  const boardsLoading = boards.some((b) => b.isLoading);
  const allBoardsFailed = boards.length > 0 && boards.every((b) => b.isError);

  // Empty state: the onboarding hero (§6 conventions — one-line explanation plus
  // a primary action).
  if (projectsQuery.isSuccess && projects.length === 0) {
    return (
      <div className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-dashed p-10 text-center">
        <Sparkles aria-hidden className="size-6 text-primary" />
        <div>
          <h1 className="text-lg font-semibold">
            Give TITAN something to build
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Describe a product in a sentence or two. The CEO agent asks what it
            genuinely needs to know, then the team plans and builds it.
          </p>
        </div>
        <Button asChild>
          <Link href="/projects/new">Start a project</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Organization Overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Company-wide health across every project (Vol 4 §6.1).
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          title="Projects"
          state={
            projectsQuery.isLoading
              ? "loading"
              : projectsQuery.isError
                ? "error"
                : "ready"
          }
          onRetry={() => void projectsQuery.refetch()}
          value={String(projects.length)}
          sub={`${active.length} active`}
          href="/projects"
        />
        <KpiCard
          title="Agents"
          state={
            agentsQuery.isLoading
              ? "loading"
              : agentsQuery.isError
                ? "error"
                : "ready"
          }
          onRetry={() => void agentsQuery.refetch()}
          value={`${countWorking(agents)}/${agents.length}`}
          sub={`${countWorking(agents)} working`}
          href="/agents"
        />
        <KpiCard
          title="Tasks"
          state={
            boardsLoading ? "loading" : allBoardsFailed ? "error" : "ready"
          }
          onRetry={() => boards.forEach((b) => void b.refetch())}
          value={String(taskTotals.total)}
          sub={
            taskTotals.failed > 0 ? (
              // Never claim "none blocked" from a partial aggregate — a project
              // whose board failed could be the one with blocked work.
              <span className="text-warning">
                {taskTotals.blocked > 0
                  ? `${taskTotals.blocked} blocked · `
                  : ""}
                {taskTotals.failed} project
                {taskTotals.failed === 1 ? "" : "s"} unknown
              </span>
            ) : taskTotals.blocked > 0 ? (
              <span className="font-medium text-warning">
                {taskTotals.blocked} blocked
              </span>
            ) : (
              "none blocked"
            )
          }
          // §6.1's example deep-link is "5 blocked -> board filtered
          // state=blocked", but the board is per-project (/projects/[pid]/board)
          // and there is no org-wide board route — so the honest destination is
          // the projects list, from which a specific board is one click away.
          href="/projects"
        />
        <CostKpiCard />
      </div>

      <AttentionApprovals projectIds={activeIds} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <section className="space-y-3">
          <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            Active projects
          </h2>
          {projectsQuery.isLoading ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <ProjectCardSkeleton />
              <ProjectCardSkeleton />
            </div>
          ) : projectsQuery.isError ? (
            <div className="rounded-lg border border-dashed p-6 text-center text-sm text-destructive">
              Couldn’t load projects.{" "}
              <button
                type="button"
                className="underline underline-offset-4"
                onClick={() => void projectsQuery.refetch()}
              >
                Retry
              </button>
            </div>
          ) : active.length === 0 ? (
            <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              No active projects.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {active.map((project, index) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  board={boards[index]?.data}
                  boardLoading={boards[index]?.isLoading}
                  agents={agentsByPid.get(project.id)}
                />
              ))}
            </div>
          )}

          {/* The notification-driven AttentionQueue is deliberately NOT rendered
              here. Approvals reach the UI as approval/critical notifications, so
              it would show the SAME item as <AttentionApprovals> above — but its
              "Resolve" only marks the notification read, which would make the
              item vanish while the agent stayed blocked. The bell still carries
              those notifications; this screen resolves them for real. */}
        </section>

        <ActivityFeed />
      </div>
    </div>
  );
}
