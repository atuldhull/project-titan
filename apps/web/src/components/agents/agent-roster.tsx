"use client";

// The 16-agent status grid (Vol 4 §6.5), wired to GET /api/v1/agent-definitions
// through the Phase-57 data layer: React Query over the typed client and the BFF
// proxy, authenticated by the httpOnly session cookie (no token paste). Runtime
// status (current task, live KPIs) joins over the WS topic in a later M6 surface.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";

import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

import { AgentCard } from "./agent-card";
import { AgentDetail } from "./agent-detail";
import {
  TIER_LABEL,
  TIER_ORDER,
  type AgentDefinition,
  type AgentTier,
} from "./types";

function groupByTier(
  agents: AgentDefinition[],
): [AgentTier, AgentDefinition[]][] {
  return TIER_ORDER.map(
    (tier) =>
      [tier, agents.filter((a) => a.tier === tier)] as [
        AgentTier,
        AgentDefinition[],
      ],
  ).filter(([, list]) => list.length > 0);
}

function useAgentDefinitions() {
  return useQuery({
    queryKey: queryKeys.agents(),
    queryFn: async () =>
      // The generated AgentDefinition DTO is the same backend contract the local
      // `./types` mirrors for the card/detail views.
      unwrap(
        await browserApi().GET("/api/v1/agent-definitions"),
      ) as unknown as AgentDefinition[],
  });
}

export function AgentRoster() {
  const { data, isLoading, isError } = useAgentDefinitions();
  const agents = useMemo(() => data ?? [], [data]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const grouped = useMemo(() => groupByTier(agents), [agents]);
  const effectiveId =
    selectedId && agents.some((a) => a.agent_id === selectedId)
      ? selectedId
      : (agents[0]?.agent_id ?? null);
  const selected = agents.find((a) => a.agent_id === effectiveId) ?? null;

  const vetoCount = agents.filter((a) => a.authority.veto).length;
  const closedCount = agents.filter(
    (a) => a.failure_policy.mode === "fail_closed",
  ).length;

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Agents</h1>
        <p className="text-sm text-muted-foreground">
          The declarative agent roster — tiers, authority, memory scopes, tool
          grants and KPIs, live from{" "}
          <code className="font-mono text-xs">
            GET /api/v1/agent-definitions
          </code>
          .
        </p>
      </header>

      {isLoading ? (
        <div className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          Loading the roster…
        </div>
      ) : isError ? (
        <div className="rounded-lg border bg-card p-6 text-sm text-destructive">
          Couldn’t load the agent roster. Check that you’re signed in and the
          API is reachable.
        </div>
      ) : agents.length === 0 ? (
        <div className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          No agent definitions found.
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="secondary">{agents.length} agents</Badge>
            <Badge variant="secondary">{grouped.length} tiers</Badge>
            <Badge variant="outline">{vetoCount} veto</Badge>
            <Badge variant="outline">{closedCount} fail-closed</Badge>
          </div>

          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
            <div className="space-y-6">
              {grouped.map(([tier, list]) => (
                <section key={String(tier)} className="space-y-3">
                  <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    {TIER_LABEL[String(tier)]}
                  </h2>
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                    {list.map((agent) => (
                      <AgentCard
                        key={agent.agent_id}
                        agent={agent}
                        selected={agent.agent_id === effectiveId}
                        onSelect={setSelectedId}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </div>
            <div className="lg:sticky lg:top-6 lg:self-start">
              {selected ? (
                <AgentDetail agent={selected} />
              ) : (
                <p className="rounded-lg border bg-card p-5 text-sm text-muted-foreground">
                  Select an agent to see its full role contract.
                </p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
