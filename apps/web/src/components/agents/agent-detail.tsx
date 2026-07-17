"use client";

/**
 * One agent in detail — the body of the §6.5 drawer and of its full page.
 *
 * §6.5 asks for "KPIs, recent messages, memory usage, and action buttons
 * (Pause, Restart, Reassign)". Against the real backend:
 *
 * - **KPIs**: the roster declares KPI *names* ("diff coverage >= 80%"). Nothing
 *   in the system ever emits a value for one. They render as what they are —
 *   declared measures, not a scoreboard. Numbers here would be the easiest lie
 *   on this screen.
 * - **Recent messages**: real, but attributed by ROLE, not by agent id — the
 *   bus writes `sender_agent_id=None` and records the sender in `from_agent`,
 *   so an id filter would show an empty list beside a chatty agent. Two agents
 *   sharing a role are therefore indistinguishable here, and the section says
 *   so rather than implying otherwise.
 * - **Memory usage**: does not exist for an agent. The roster's `memory_scopes`
 *   (what it may read and write) is the real adjacent fact, shown under its own
 *   accurate name rather than dressed up as usage.
 * - **Actions**: Pause/Resume are real and admin-gated. Restart and Reassign
 *   have no endpoints, so they are not rendered as dead buttons.
 */

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { StateChip } from "@/components/agents/state-chip";
import {
  formatElapsed,
  formatThroughput,
} from "@/components/agents/agent-card";
import { useAgentLifecycle, useAgentMessages } from "@/lib/agents/hooks";
import { useCan } from "@/lib/auth/use-can";
import type { AgentView } from "@/lib/agents/view";
import { MODEL_TIER_VARIANT, type ProjectRun } from "@/lib/agents/types";

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Chips({ items }: { items: readonly string[] }) {
  if (items.length === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <Badge key={item} variant="outline" className="font-mono text-xs">
          {item}
        </Badge>
      ))}
    </div>
  );
}

function Actions({ view }: { view: AgentView }) {
  // `agent:configure` is held by owner and admin only — §6.5's "RBAC admin+".
  // The server re-checks every mutation; this only avoids offering a button
  // that would 403.
  const canConfigure = useCan("agent:configure");
  const { pause, resume } = useAgentLifecycle();
  const paused = view.agent.status === "paused";
  const busy = pause.isPending || resume.isPending;

  if (!canConfigure) {
    return null;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {paused ? (
        <Button
          size="sm"
          disabled={busy}
          onClick={() => resume.mutate(view.agent.id)}
        >
          Resume
        </Button>
      ) : (
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            // §6.5 wants a confirmation. Pausing stops an agent taking work, so
            // it earns one — but it is reversible with the button that replaces
            // it, so a native confirm is proportionate.
            if (
              window.confirm(
                `Pause ${view.agent.name}? It will stop taking new work.`,
              )
            ) {
              pause.mutate(view.agent.id);
            }
          }}
        >
          Pause
        </Button>
      )}
    </div>
  );
}

export function AgentDetail({
  view,
  runs,
}: {
  view: AgentView;
  /** This agent's runs, already gathered across every project it could have
   *  worked in — an org-level agent's are not in "its" project, because it has
   *  none. Passed in rather than re-fetched so the drawer cannot disagree with
   *  the grid that opened it. */
  runs: { runs: readonly ProjectRun[]; isLoading: boolean; isError: boolean };
}) {
  const { agent, definition } = view;
  const mine = runs.runs.filter((r) => r.agent_id === agent.id);
  // The project to read messages from and to link tasks into. An org-level
  // agent's own `project_id` is NULL, yet its runs still live in a real
  // project — so ask the work where it is, and fall back to the column only
  // when it has not run.
  const activeProjectId = view.runProjectId ?? agent.project_id;
  // Messages are attributed by ROLE, never by agent id (see useAgentMessages).
  const messages = useAgentMessages(activeProjectId, agent.roster_id);

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">{agent.name}</h2>
          <StateChip state={view.state} />
        </div>
        <p className="font-mono text-xs text-muted-foreground">
          {agent.role}
          {agent.roster_id ? ` · ${agent.roster_id}` : " · no roster contract"}
          {definition ? ` · reports to ${definition.supervisor}` : ""}
        </p>
        <Actions view={view} />
      </header>

      <Section title="Now">
        {view.failure ? (
          <p className="text-sm text-destructive">
            {view.failure.error ?? "The last run failed."}
          </p>
        ) : view.currentTaskId && activeProjectId ? (
          <Link
            href={`/projects/${activeProjectId}/board?task=${view.currentTaskId}`}
            className="text-sm underline underline-offset-2"
          >
            Working on task {view.currentTaskId.slice(0, 8)}
          </Link>
        ) : (
          <p className="text-sm text-muted-foreground">No active task.</p>
        )}
        <div className="flex flex-wrap gap-1.5">
          {view.model.model ? (
            <Badge variant="outline" className="font-mono text-xs">
              {view.model.provider ? `${view.model.provider}/` : ""}
              {view.model.model}
              {view.model.actual ? "" : " (configured)"}
            </Badge>
          ) : null}
          {view.elapsedMs !== null ? (
            <Badge variant="secondary">
              {formatElapsed(view.elapsedMs)} elapsed
            </Badge>
          ) : null}
          {view.throughput ? (
            <Badge
              variant="secondary"
              title="Average over the last completed run"
            >
              {formatThroughput(view.throughput.tokensPerSecond)} (last run)
            </Badge>
          ) : null}
        </div>
      </Section>

      {/* TITAN-FRT-027's "one-click path to its logs". A run IS the execution
          record: the model used, tokens, timings, output summary and error. */}
      <Section title="Runs">
        {runs.isLoading ? (
          <Skeleton className="h-16 rounded-md" />
        ) : runs.isError ? (
          <p className="text-xs text-destructive">
            Couldn’t load this agent’s runs.
          </p>
        ) : mine.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            This agent has not run yet.
          </p>
        ) : (
          <ul className="space-y-1">
            {mine.slice(0, 8).map((run) => (
              <li
                key={run.id}
                className="flex items-center justify-between gap-2 text-xs"
              >
                <span className="flex min-w-0 items-center gap-1.5">
                  <Badge
                    variant={
                      run.status === "failed" || run.status === "timed_out"
                        ? "destructive"
                        : run.status === "succeeded"
                          ? "secondary"
                          : "outline"
                    }
                  >
                    {run.status}
                  </Badge>
                  <Link
                    // Each run carries the project it was fetched from, so this
                    // is right even for an org-level agent working across
                    // several projects at once.
                    href={`/projects/${run.project_id}/board?task=${run.task_id}`}
                    className="truncate font-mono text-muted-foreground underline-offset-2 hover:underline"
                  >
                    {run.task_id.slice(0, 8)}
                  </Link>
                  <span className="text-muted-foreground">#{run.attempt}</span>
                </span>
                <span className="shrink-0 font-mono text-muted-foreground">
                  {(run.tokens_out ?? 0) > 0 ? `${run.tokens_out} tok` : "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {/* Attributed by ROLE — the bus records the sender in `from_agent` and
          leaves `sender_agent_id` null, so this is what this agent's role has
          been saying in this project, not provably this agent alone. */}
      <Section
        title={
          agent.roster_id
            ? `Recent messages from ${agent.roster_id}`
            : "Recent messages"
        }
      >
        {activeProjectId === null ? (
          <p className="text-xs text-muted-foreground">
            This agent has no work in any project yet, so there is nothing to
            show.
          </p>
        ) : agent.roster_id === null ? (
          <p className="text-xs text-muted-foreground">
            This agent’s role has no roster id, and the bus attributes messages
            by role — so its output cannot be identified.
          </p>
        ) : messages.isLoading ? (
          <Skeleton className="h-16 rounded-md" />
        ) : messages.isError ? (
          <p className="text-xs text-destructive">
            Couldn’t load this agent’s messages.
          </p>
        ) : (messages.data?.items.length ?? 0) === 0 ? (
          <p className="text-xs text-muted-foreground">
            This agent has not posted yet.
          </p>
        ) : (
          <ul className="space-y-1">
            {messages.data?.items.slice(0, 6).map((message) => (
              <li
                key={message.id}
                className="flex items-baseline gap-1.5 text-xs"
              >
                <Badge variant="outline" className="shrink-0 font-mono">
                  {message.kind}
                </Badge>
                <span className="truncate text-muted-foreground">
                  {/* The log is LOSSY: many actions collapse onto one kind, so
                      what happened is body.action, never kind. */}
                  {typeof message.body?.action === "string"
                    ? message.body.action
                    : "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {definition ? (
        <>
          <Section title="KPIs">
            <Chips items={definition.kpis} />
            <p className="text-xs text-muted-foreground">
              Declared measures — nothing in the system reports values for
              these.
            </p>
          </Section>

          <Section title="Memory scopes">
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground">reads</p>
              <Chips items={definition.memory_scopes.read} />
              <p className="text-xs text-muted-foreground">writes</p>
              <Chips items={definition.memory_scopes.write} />
            </div>
          </Section>

          <Section title="Model policy">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant={MODEL_TIER_VARIANT[definition.model_policy.tier]}>
                {definition.model_policy.tier}
              </Badge>
              <Badge variant="outline">
                temp {definition.model_policy.temperature}
              </Badge>
              <Badge variant="outline">
                max {definition.model_policy.max_output_tokens} out
              </Badge>
              {definition.authority.veto ? (
                <Badge variant="destructive">veto</Badge>
              ) : null}
            </div>
          </Section>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          This agent’s role has no roster contract, so it has no declared tier,
          KPIs or authority.
        </p>
      )}
    </div>
  );
}
