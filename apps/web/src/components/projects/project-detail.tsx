"use client";

import { useEffect, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PluginSlot } from "@/components/plugins/plugin-slot";
import { StubPage } from "@/components/shell/stub-page";
import { boardStats } from "@/lib/projects/derive";
import { useProject, useProjectBoard, useSprints } from "@/lib/projects/hooks";
import { useTopic } from "@/lib/ws/use-topic";

import { ArtifactPanel } from "./artifact-panel";
import { MilestoneTimeline } from "./milestone-timeline";

/** §6.2's tab set, in order. Tabs owned by later phases render labelled stubs —
 *  allowed for PAGES only, never backend code. */
const TABS = [
  "overview",
  "board",
  "graph",
  "architecture",
  "deployments",
  "files",
  "settings",
] as const;
type Tab = (typeof TABS)[number];

function isTab(value: string | null): value is Tab {
  return value !== null && (TABS as readonly string[]).includes(value);
}

export function ProjectDetail({ projectId }: { projectId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("tab");
  // `board` is a tab TRIGGER but not a tab PANE — it routes to the real board
  // (see setTab). A bookmarked `?tab=board` must therefore go there too, not
  // select a pane that does not exist and render blank. Fall back to overview
  // for the render that precedes the redirect.
  const tab: Tab =
    isTab(requested) && requested !== "board" ? requested : "overview";

  useEffect(() => {
    if (requested === "board") {
      router.replace(`/projects/${projectId}/board`);
    }
  }, [requested, projectId, router]);

  // Live: task/pipeline frames for this project keep the caches fresh.
  useTopic("projectTasks", projectId);
  useTopic("projectRuns", projectId);

  const project = useProject(projectId);
  const board = useProjectBoard(projectId);
  const sprints = useSprints(projectId);

  const stats = useMemo(() => boardStats(board.data), [board.data]);
  // No "current sprint" endpoint and no ?status filter — pick the active one
  // client-side, else the highest-numbered.
  const sprint = useMemo(() => {
    const all = sprints.data?.items ?? [];
    return (
      all.find((s) => s.status === "active") ??
      [...all].sort((a, b) => b.number - a.number)[0]
    );
  }, [sprints.data]);

  const setTab = (next: string) => {
    // The board is a real route of its own (Vol 4 §6.3/§11), not a tab pane.
    // Route to it rather than render a second copy here: two board URLs would
    // mean two homes for the `?task=` deep link and two live subscriptions.
    if (next === "board") {
      router.push(`/projects/${projectId}/board`);
      return;
    }
    const qs = new URLSearchParams(params.toString());
    if (next === "overview") {
      qs.delete("tab");
    } else {
      qs.set("tab", next);
    }
    // Navigational state is URL-encoded and restorable on reload (TITAN-FRT-006).
    router.replace(`/projects/${projectId}${qs.size ? `?${qs}` : ""}`, {
      scroll: false,
    });
  };

  if (project.isError) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-destructive">
        Couldn’t load this project.{" "}
        <button
          type="button"
          className="underline underline-offset-4"
          onClick={() => void project.refetch()}
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <header className="space-y-2">
        {project.isLoading ? (
          <Skeleton className="h-6 w-56" />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold">
              {project.data?.display_name}
            </h1>
            <Badge variant="outline" className="capitalize">
              {project.data?.status}
            </Badge>
            {sprint ? (
              <span className="text-xs text-muted-foreground">
                sprint {sprint.number}
              </span>
            ) : null}
            <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
              <Progress value={stats.progressPct} className="w-24" />
              {stats.progressPct === null ? "—" : `${stats.progressPct}%`}
            </span>
          </div>
        )}
        {/* §6.2 wants Pause/Resume gating all agent work. There is NO
            project-level pause/resume endpoint (only per-agent :pause/:resume),
            so a button here would be a lie — omitted deliberately until the
            backend grows one, rather than shipping a control that does nothing
            or silently pauses agents one-by-one with no atomicity. */}
      </header>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {TABS.map((name) => (
            <TabsTrigger key={name} value={name} className="capitalize">
              {name}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <MilestoneTimeline projectId={projectId} />
            <ArtifactPanel
              projectId={projectId}
              kind="roadmap"
              title="Roadmap"
              emptyHint="No roadmap yet. It is generated from the planning handoffs once the CEO vision and PM plan land."
            />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <ArtifactPanel
              projectId={projectId}
              kind="risk_analysis"
              title="Risk register"
              emptyHint="No risk analysis yet — it is derived once the project has a plan and clarifications."
            />
            <ArtifactPanel
              projectId={projectId}
              kind="debt_report"
              title="Tech-debt report"
              emptyHint="No tech-debt report yet — it is derived once reviews and gates have run."
            />
          </div>
        </TabsContent>

        {/* No "board" TabsContent: selecting that tab navigates to
            /projects/{pid}/board, the canonical board route. */}
        <TabsContent value="graph">
          <StubPage
            title="Dependency graph"
            description="The task/architecture graph canvas (Vol 4 §6.4)."
            milestone="Phase 60"
          />
        </TabsContent>
        <TabsContent value="architecture">
          <StubPage
            title="Architecture"
            description="Architecture diagrams and ADRs (Vol 4 §6.4)."
            milestone="Phase 60"
          />
        </TabsContent>
        <TabsContent value="deployments">
          <StubPage
            title="Deployments"
            description="Environments, pipeline runs and stage status (Vol 4 §6.9)."
            milestone="Phase 62"
          />
        </TabsContent>
        <TabsContent value="files">
          <StubPage
            title="Files"
            description="The project workspace file tree and artifacts (Vol 4 §6.8)."
            milestone="a later M6 phase"
          />
        </TabsContent>
        <TabsContent value="settings">
          <StubPage
            title="Project settings"
            description="Per-project configuration (Vol 4 §6.13)."
            milestone="a later M6 phase"
          />
        </TabsContent>
      </Tabs>

      {/* Named extension slot (TITAN-INT-011): every enabled plugin declaring
          `mount: project.sidebar` renders here, from its manifest alone. This
          one line is the ONLY core change a plugin's UI ever needs — the panel
          itself is plugin-authored HTML in a sandboxed frame. Renders nothing
          at all when no plugin mounts here. */}
      <PluginSlot name="project.sidebar" className="space-y-3" />
    </div>
  );
}
