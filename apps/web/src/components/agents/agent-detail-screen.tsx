"use client";

/**
 * Loads ONE agent's view and renders `AgentDetail`.
 *
 * Shared by both surfaces the drawer route needs: the intercepted slide-over
 * and the full page you land on with a hard navigation or a shared link. Both
 * must show the same thing, so both render this.
 */

import { useMemo } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { AgentDetail } from "@/components/agents/agent-detail";
import {
  useAgentDefinitions,
  useAgents,
  useRunsAcrossProjects,
} from "@/lib/agents/hooks";
import { useNowMs } from "@/lib/agents/use-now";
import { definitionsById } from "@/lib/agents/roster";
import { agentView, runProjectIds } from "@/lib/agents/view";
import { useProjects } from "@/lib/projects/hooks";
import { useProjectTopics } from "@/lib/ws/use-topic";

export function AgentDetailScreen({ agentId }: { agentId: string }) {
  const agents = useAgents();
  const definitions = useAgentDefinitions();
  const projects = useProjects();

  const agent = useMemo(
    () => agents.data?.items.find((a) => a.id === agentId) ?? null,
    [agents.data, agentId],
  );

  // Same rule as the grid: an org-level agent (project_id null) can own runs in
  // ANY project, so its runs cannot be found by looking only where it "belongs".
  const allProjectIds = useMemo(
    () => (projects.data?.items ?? []).map((p) => p.id),
    [projects.data],
  );
  const projectIds = useMemo(
    () => (agent ? runProjectIds([agent], allProjectIds) : []),
    [agent, allProjectIds],
  );
  useProjectTopics("projectRuns", projectIds);

  const runs = useRunsAcrossProjects(projectIds);
  const nowMs = useNowMs();

  const view = useMemo(() => {
    if (!agent) {
      return null;
    }
    const mine = runs.runs.filter((r) => r.agent_id === agent.id);
    return agentView(
      agent,
      mine,
      definitionsById(definitions.data ?? []),
      nowMs,
    );
  }, [agent, runs.runs, definitions.data, nowMs]);

  if (agents.isLoading || definitions.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-24 rounded-md" />
      </div>
    );
  }
  if (agents.isError) {
    return (
      <p className="text-sm text-destructive">Couldn’t load this agent.</p>
    );
  }
  if (!view) {
    return <p className="text-sm text-muted-foreground">No such agent.</p>;
  }
  return <AgentDetail view={view} runs={runs} />;
}
