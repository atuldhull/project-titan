"use client";

/**
 * The task dependency graph (Vol 4 §6.4, TITAN-FRT-026).
 *
 * Deviations from §6.4, each forced and each documented at its cause:
 * - **No agent avatar without a second fetch.** `GraphNode` is `{id, title,
 *   status}`; the assignee lives on `tasks` but the graph SELECT never projects
 *   it. The task list is joined here, and an unknown assignee renders as unknown.
 * - **No "bulk label ops" behind Shift+click.** Tasks have no labels — no column,
 *   no table, no endpoint, in any migration — and there is no bulk mutation of
 *   any kind (every task write is single, and PATCH demands its own If-Match).
 *   Multi-select still SELECTS, because inspecting several nodes is useful and
 *   honest; it offers no bulk action, because there is none to offer.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { GitBranch } from "lucide-react";

import { GraphShell } from "@/components/graph/graph-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { BoardEmpty, BoardError } from "@/components/graph/graph-states";
import { CanvasSkeleton } from "@/components/graph/canvas";
import { useProjectTasks } from "@/lib/graph/hooks";
import { useProjectGraph } from "@/lib/kanban/hooks";
// `/agents` has no project filter, so this is the whole roster; the screen picks
// out the assignees it needs. Reused rather than re-declared — one agents query
// per tab is enough.
import { useAgents } from "@/lib/projects/hooks";
import { adjacency, connectedChains } from "@/lib/graph/model";
import {
  criticalPath,
  milestoneGroup,
  taskGraphModel,
} from "@/lib/graph/task-graph";
import { initials } from "@/lib/agents/initials";
import { useTopic } from "@/lib/ws/use-topic";

export function TaskGraphScreen({ projectId }: { projectId: string }) {
  const router = useRouter();
  const graph = useProjectGraph(projectId);
  const tasks = useProjectTasks(projectId);
  const agents = useAgents();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [alsoSelected, setAlsoSelected] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [showPath, setShowPath] = useState(false);

  // Task frames already invalidate this project's graph query — `project(pid)`
  // is a prefix of the graph key (pinned in tests/lib/ws/reconcile.test.ts). So
  // subscribing is all that is needed for §6.4's "status patches recolor in
  // place": the refetch arrives and `incrementalLayout` holds every position.
  useTopic("projectTasks", projectId);

  // --- deep link: ?node= ----------------------------------------------------
  useEffect(() => {
    const read = (): void =>
      setSelectedId(new URLSearchParams(window.location.search).get("node"));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  const select = useCallback((id: string | null, additive: boolean): void => {
    if (additive && id !== null) {
      setAlsoSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) {
          next.delete(id);
        } else {
          next.add(id);
        }
        return next;
      });
      return;
    }
    setAlsoSelected(new Set());
    const url = new URL(window.location.href);
    if (id === null) {
      url.searchParams.delete("node");
    } else {
      url.searchParams.set("node", id);
    }
    // replaceState, not push: clicking through 20 nodes must not bury the back
    // button under 20 entries.
    window.history.replaceState({}, "", url);
    setSelectedId(id);
  }, []);

  const assignees = useMemo(
    () =>
      new Map(
        (tasks.data?.items ?? []).map((t) => [
          t.id,
          t.assignee_agent_id ?? null,
        ]),
      ),
    [tasks.data],
  );
  const milestones = useMemo(
    () =>
      new Map(
        (tasks.data?.items ?? []).map((t) => [t.id, t.milestone_id ?? null]),
      ),
    [tasks.data],
  );
  const agentsById = useMemo(
    () => new Map((agents.data?.items ?? []).map((a) => [a.id, a])),
    [agents.data],
  );

  const model = useMemo(
    () => taskGraphModel(graph.data, { assignees, milestones }),
    [graph.data, assignees, milestones],
  );
  const path = useMemo(
    () => (showPath ? new Set(criticalPath(model)) : new Set<string>()),
    [showPath, model],
  );
  const adj = useMemo(() => adjacency(model), [model]);
  // §6.4 asks for the upstream/downstream chains to highlight on HOVER. They
  // highlight on SELECTION instead — deliberately, and it is a superset: a click,
  // a Tab, and a List-view row all produce the same highlight, whereas hover is
  // unreachable from the keyboard and meaningless on touch. What is genuinely
  // lost is the no-click preview.
  const spotlight = useMemo(
    () => (selectedId ? connectedChains(adj, selectedId) : null),
    [adj, selectedId],
  );

  const selected = selectedId
    ? model.nodes.find((n) => n.id === selectedId)
    : undefined;

  if (graph.isLoading) {
    return <CanvasSkeleton />;
  }
  if (graph.isError) {
    return <BoardError onRetry={() => void graph.refetch()} />;
  }
  if (model.nodes.length === 0) {
    return <BoardEmpty />;
  }

  const blockers = selected ? (adj.in.get(selected.id) ?? []) : [];
  const agent = selected?.data.assigneeAgentId
    ? agentsById.get(selected.data.assigneeAgentId)
    : undefined;

  return (
    <GraphShell
      model={model}
      selectedId={selectedId}
      alsoSelected={alsoSelected}
      onSelect={select}
      // §6.4: double-click opens the task Sheet. The Sheet lives on the board —
      // the graph is read-only, and dependency changes are requested there.
      onActivate={(id) =>
        router.push(`/projects/${projectId}/board?task=${id}`)
      }
      // TITAN-FRT-047: the List view offers the same action, so it has to name
      // it — "Open" alone would not say that the row leaves the graph entirely.
      activateLabel="Open on the board"
      accented={path}
      spotlight={spotlight}
      groupOf={milestoneGroup}
      exportName={`task-graph-${projectId.slice(0, 8)}`}
      emptyLabel="No tasks yet — the Project Manager Agent creates tasks after planning completes."
      toolbar={
        <>
          <Button
            variant={showPath ? "default" : "outline"}
            size="sm"
            aria-pressed={showPath}
            onClick={() => setShowPath((v) => !v)}
          >
            <GitBranch aria-hidden className="mr-1 size-3" />
            Critical path
          </Button>
          {showPath ? (
            <span className="text-xs text-muted-foreground">
              {path.size === 0
                ? "Nothing left to do."
                : `${path.size} tasks — the longest chain of remaining work.`}
            </span>
          ) : null}
        </>
      }
      inspector={
        selected ? (
          // §6.4's selection inspector bar.
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-2 text-sm">
            <span className="font-mono text-xs text-muted-foreground">
              {selected.id.slice(0, 8)}
            </span>
            <span className="font-medium">{selected.label}</span>
            <Badge variant="secondary">
              {selected.data.status.replace(/_/g, " ")}
            </Badge>
            {blockers.length > 0 ? (
              <span className="text-xs text-muted-foreground">
                blocked by{" "}
                {blockers
                  .map(
                    (id) => model.nodes.find((n) => n.id === id)?.label ?? id,
                  )
                  .join(", ")}
              </span>
            ) : null}
            {agent ? (
              <span className="inline-flex items-center gap-1 text-xs">
                <span className="inline-flex size-5 items-center justify-center rounded-full bg-secondary text-[9px] font-semibold text-secondary-foreground">
                  {initials(agent.name)}
                </span>
                {agent.name}
              </span>
            ) : tasks.isLoading || tasks.isError ? (
              // The graph endpoint carries no assignee — this comes from the task
              // list. Until that resolves the honest word is "unknown": saying
              // "Unassigned" from data we have not fetched asserts something
              // about the task rather than about ourselves.
              <span className="text-xs text-muted-foreground">
                Assignee unknown
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">Unassigned</span>
            )}
            {/* §6.4: humans request dependency changes through the task Sheet,
                which lives on the board — the graph is read-only. */}
            <Link
              href={`/projects/${projectId}/board?task=${selected.id}`}
              className="ml-auto text-xs underline"
            >
              Open
            </Link>
            {alsoSelected.size > 0 ? (
              <span className="text-xs text-muted-foreground">
                +{alsoSelected.size} selected
              </span>
            ) : null}
          </div>
        ) : null
      }
    />
  );
}
