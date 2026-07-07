"use client";

// The 16-agent status grid (Vol 4 §6.5), wired live to the Phase-26 roster
// endpoint GET /api/v1/agent-definitions. This is the first M6 dashboard surface
// brought forward: it reads the declarative AgentDefinition roster and lays the
// org out by tier, with each role's authority, memory scopes, tool grants and
// KPIs on selection. Runtime status (current task, live KPIs) joins in M6 once
// the realtime data layer (Phase 57) lands; for now it presents the contracts.

import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import { AgentCard } from "./agent-card";
import { AgentDetail } from "./agent-detail";
import {
  TIER_LABEL,
  TIER_ORDER,
  type AgentDefinition,
  type AgentTier,
} from "./types";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "titan.access_token";

type LoadState = "idle" | "loading" | "ready" | "error";

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

export function AgentRoster() {
  const [token, setToken] = useState("");
  const [draft, setDraft] = useState("");
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Reuse a token pasted on any surface (no frontend auth flow until Phase 8/57).
  useEffect(() => {
    const saved = window.localStorage.getItem(TOKEN_KEY);
    if (saved) {
      setToken(saved);
    }
  }, []);

  useEffect(() => {
    if (!token) {
      return;
    }
    const controller = new AbortController();
    setState("loading");
    setError(null);
    fetch(`${API}/api/v1/agent-definitions`, {
      headers: { authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (res) => {
        if (res.status === 401 || res.status === 403) {
          throw new Error("Token rejected — paste a fresh access token.");
        }
        if (!res.ok) {
          throw new Error(`Request failed (${res.status}).`);
        }
        return (await res.json()) as AgentDefinition[];
      })
      .then((data) => {
        setAgents(data);
        setSelectedId((current) => current ?? data[0]?.agent_id ?? null);
        setState("ready");
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        setError(err instanceof Error ? err.message : "Request failed.");
        setState("error");
      });
    return () => controller.abort();
  }, [token]);

  const applyToken = useCallback(() => {
    const next = draft.trim();
    if (!next) {
      return;
    }
    window.localStorage.setItem(TOKEN_KEY, next);
    setDraft("");
    setToken(next);
  }, [draft]);

  const grouped = useMemo(() => groupByTier(agents), [agents]);
  const selected = agents.find((a) => a.agent_id === selectedId) ?? null;

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

      {state !== "ready" || error ? (
        <div className="space-y-3 rounded-lg border bg-card p-6">
          <div>
            <h2 className="text-sm font-medium">Access token</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              The roster needs an authenticated read. Paste an access token from{" "}
              <code className="font-mono">POST /api/v1/auth/token</code> — it is
              remembered in this browser.
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              type="password"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  applyToken();
                }
              }}
              placeholder="paste access token (JWT)"
              className="w-full rounded-md border bg-background px-3 py-2 font-mono text-xs"
            />
            <Button type="button" onClick={applyToken} disabled={!draft.trim()}>
              {state === "loading" ? "Loading…" : "Load roster"}
            </Button>
          </div>
          {error ? <p className="text-xs text-destructive">{error}</p> : null}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="secondary">{agents.length} agents</Badge>
          <Badge variant="secondary">{grouped.length} tiers</Badge>
          <Badge variant="outline">{vetoCount} veto</Badge>
          <Badge variant="outline">{closedCount} fail-closed</Badge>
          <button
            type="button"
            onClick={() => {
              window.localStorage.removeItem(TOKEN_KEY);
              setToken("");
              setAgents([]);
              setState("idle");
            }}
            className="ml-auto underline underline-offset-4 hover:text-foreground"
          >
            change token
          </button>
        </div>
      )}

      {state === "ready" && agents.length > 0 ? (
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
                      selected={agent.agent_id === selectedId}
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
      ) : null}
    </div>
  );
}
