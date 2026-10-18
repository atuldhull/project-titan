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

// Tabs backed by a real screen at their OWN sub-route (Phases 59/60/62): selecting
// the tab navigates there rather than rendering an inline pane, so there is one home
// per screen (and one `?task=` / live-subscription owner). Only overview/files/settings
// render as inline panes here (files/settings have no backend screen yet).
const ROUTED = new Set<string>([
  "board",
  "graph",
  "architecture",
  "deployments",
]);

export function ProjectDetail({ projectId }: { projectId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("tab");
  // A ROUTED tab is a TRIGGER but not a PANE — it routes to the real screen (see
  // setTab). A bookmarked `?tab=graph` must therefore go there too, not select a
  // pane that does not exist and render blank. Fall back to overview for the render
  // that precedes the redirect.
  const tab: Tab =
    isTab(requested) && !ROUTED.has(requested) ? requested : "overview";

  useEffect(() => {
    if (isTab(requested) && ROUTED.has(requested)) {
      router.replace(`/projects/${projectId}/${requested}`);
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
    // A routed tab (board/graph/architecture/deployments) is a real screen at its
    // own route (Vol 4 §6.3/§6.4/§6.9), not a tab pane. Navigate to it rather than
    // render a second copy here, so each screen has one URL, one `?task=`/`?node=`
    // deep-link owner, and one live subscription.
    if (ROUTED.has(next)) {
      router.push(`/projects/${projectId}/${next}`);
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
              <Progress
                label="Task completion"
                value={stats.progressPct}
                className="w-24"
              />
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

        {/* No board/graph/architecture/deployments TabsContent: those triggers are
            ROUTED (see setTab) to their real screens at /projects/{pid}/{tab}. */}
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
