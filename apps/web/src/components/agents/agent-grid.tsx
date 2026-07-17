"use client";

/**
 * The Agent Status Grid (Vol 4 §6.5, TITAN-FRT-027) — "the org chart in
 * motion".
 *
 * What is real here, and what §6.5 asked for that has no backend:
 *
 * - **Real**: the roster and its tiers, each agent's state, the task it is on,
 *   the model and provider really in use, elapsed time, the error text, and the
 *   one-click path to that agent's runs. All of it comes from `runs` — see
 *   `lib/agents/derive` for why the `agents` table cannot answer any of it.
 * - **Not real — no heartbeat exists.** §6.5 wants 5s heartbeats and "3 missed
 *   -> stale with a last-seen time". There is no heartbeat emitter anywhere in
 *   the backend: `agent.status` fires from exactly two call sites, pause and
 *   resume. A per-agent staleness timer would be a countdown against a clock
 *   nothing winds. What IS real is whether OUR SOCKET is live — that is the
 *   honest form of "this data may be stale", so the header says it and the
 *   cards keep their last-known values. The grid never blanks either way.
 * - **Not real**: Restart and Reassign (no endpoints); token throughput as a
 *   LIVE rate (tokens are written once, at finalize).
 *
 * Two things here are polled rather than pushed, and each because a frame never
 * comes: a run that TIMES OUT emits no event at all (the coordinator marks it
 * and nacks), and elapsed needs a clock nothing else winds. So runs refetch on a
 * slow interval and the view takes its `now` from `useNowMs`. The socket is
 * still the fast path — it is what makes an error card appear within 2s
 * (TITAN-FRT-027); the poll is the floor under it.
 */

import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { AgentCard } from "@/components/agents/agent-card";
import {
  useAgentDefinitions,
  useAgents,
  useRunsAcrossProjects,
} from "@/lib/agents/hooks";
import type { AgentState } from "@/lib/agents/derive";
import {
  agentViews,
  filterViews,
  groupViews,
  needsAllProjects,
  rolesPresent,
  rollupOf,
  runProjectIds,
} from "@/lib/agents/view";
import { useNowMs } from "@/lib/agents/use-now";
import { useProjects } from "@/lib/projects/hooks";
import { useConnection } from "@/lib/ws/connection-store";
import { useProjectTopics } from "@/lib/ws/use-topic";

const STATES: AgentState[] = ["working", "idle", "paused", "error"];

function Rollup({ counts }: { counts: Record<AgentState, number> }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
      <span>
        <strong className="text-foreground">{counts.working}</strong> working
      </span>
      <span aria-hidden>·</span>
      <span>
        <strong className="text-foreground">{counts.idle}</strong> idle
      </span>
      {counts.paused > 0 ? (
        <>
          <span aria-hidden>·</span>
          <span>
            <strong className="text-foreground">{counts.paused}</strong> paused
          </span>
        </>
      ) : null}
      <span aria-hidden>·</span>
      <span className={counts.error > 0 ? "text-destructive" : undefined}>
        <strong
          className={counts.error > 0 ? "text-destructive" : "text-foreground"}
        >
          {counts.error}
        </strong>{" "}
        error
      </span>
    </div>
  );
}

function GridSkeleton() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: 8 }, (_, i) => (
        <Skeleton key={i} className="h-28 rounded-lg" />
      ))}
    </div>
  );
}

export function AgentGrid() {
  const agents = useAgents();
  const definitions = useAgentDefinitions();
  const [role, setRole] = useState<string | null>(null);
  const [state, setState] = useState<AgentState | null>(null);

  const agentList = useMemo(() => agents.data?.items ?? [], [agents.data]);

  // An org-level agent (project_id null) can own runs in ANY project, so the
  // grid cannot derive its fetch list from the agents alone — doing so renders
  // every such agent permanently idle. Only ask for the project list when one
  // is actually present.
  const wantAllProjects = needsAllProjects(agentList);
  const projects = useProjects();
  const allProjectIds = useMemo(
    () => (projects.data?.items ?? []).map((p) => p.id),
    [projects.data],
  );
  const projectIds = useMemo(
    () => runProjectIds(agentList, wantAllProjects ? allProjectIds : []),
    [agentList, wantAllProjects, allProjectIds],
  );

  // Runs are project-scoped, so an org-wide grid subscribes to each project it
  // reads. `run.started`/`run.finished` already invalidate projectRuns, which is
  // what makes a card go error within 2s of a failure (TITAN-FRT-027) without
  // waiting for the poll.
  useProjectTopics("projectRuns", projectIds);
  const runs = useRunsAcrossProjects(projectIds);

  // The honest staleness signal: not a per-agent heartbeat (none exists), but
  // whether we are receiving anything at all.
  const { status: connection } = useConnection();

  // Its own clock. Query data cannot supply one: nothing invalidates between
  // run.started and run.finished, and structural sharing means even a refetch
  // returning identical bytes leaves this memo's deps identical — so a
  // Date.now() sampled here would freeze at ~0s for the whole run.
  const nowMs = useNowMs();
  const views = useMemo(
    () => agentViews(agentList, definitions.data ?? [], runs.runs, nowMs),
    [agentList, definitions.data, runs.runs, nowMs],
  );
  const shown = useMemo(
    () => filterViews(views, { role, state }),
    [views, role, state],
  );
  const groups = useMemo(() => groupViews(shown), [shown]);
  // Unfiltered on purpose — the header describes the org, not the filter.
  const counts = useMemo(() => rollupOf(views), [views]);
  const roles = useMemo(() => rolesPresent(views), [views]);

  // Hold the skeleton until the data the cards ASSERT things from has landed.
  // Gating on `agents` alone paints a confident lie for a beat: every card
  // "idle" under a header reading "0 working · 16 idle · 0 error" (runs not in
  // yet) with all 16 filed under "Cross-cutting" (definitions not in yet),
  // before snapping to the truth. A skeleton says "I don't know"; that said
  // something false.
  if (agents.isLoading || definitions.isLoading || runs.isLoading) {
    return <GridSkeleton />;
  }
  if (agents.isError) {
    return (
      <div className="rounded-lg border bg-card p-6 text-sm text-destructive">
        Couldn’t load the agents. Check that you’re signed in and the API is
        reachable.
      </div>
    );
  }
  if (agentList.length === 0) {
    return (
      <div className="space-y-2 rounded-lg border bg-card p-6 text-sm text-muted-foreground">
        <p>No agents provisioned.</p>
        {/* §6.5's copy is "start a project to spawn the organization". It is not
            true: `create_agent` has exactly one caller in the whole backend —
            POST /api/v1/agents — and nothing in project creation or intake ever
            reaches it. Sending someone to /projects/new would leave them back
            here, still empty, with no idea why. */}
        <p>
          Agents are registered explicitly through{" "}
          <code className="font-mono text-xs">POST /api/v1/agents</code>;
          creating a project does not spawn them.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">Agents</h1>
          <Rollup counts={counts} />
        </div>
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="agent-role">
            Filter by role
          </label>
          <select
            id="agent-role"
            className="rounded-md border bg-background px-2 py-1 text-xs"
            value={role ?? ""}
            onChange={(e) => setRole(e.target.value || null)}
          >
            <option value="">All roles</option>
            {roles.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="agent-state">
            Filter by state
          </label>
          <select
            id="agent-state"
            className="rounded-md border bg-background px-2 py-1 text-xs"
            value={state ?? ""}
            onChange={(e) =>
              setState((e.target.value || null) as AgentState | null)
            }
          >
            <option value="">All states</option>
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </header>

      {connection !== "live" ? (
        <p
          role="status"
          className="rounded-md border border-dashed p-2 text-xs text-muted-foreground"
        >
          Not receiving live updates — the states below are the last we saw.
          {/* Deliberately NOT "3 missed heartbeats": no heartbeat exists to
              miss. This is the real signal, stated as what it is. */}
        </p>
      ) : null}

      {runs.isError ? (
        <p
          role="status"
          className="rounded-md border border-dashed p-2 text-xs text-muted-foreground"
        >
          Some projects’ runs couldn’t be loaded, so those agents may read as
          idle.
        </p>
      ) : null}

      {shown.length === 0 ? (
        <p className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          No agents match this filter.
        </p>
      ) : (
        groups.map((group) => (
          <section key={String(group.tier)} className="space-y-2">
            <h2 className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              {group.label}
              <Badge variant="secondary">{group.items.length}</Badge>
            </h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {group.items.map((view) => (
                <AgentCard key={view.agent.id} view={view} />
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
