"use client";

import { Badge } from "@/components/ui/badge";

import { MODEL_TIER_VARIANT, type AgentDefinition } from "./types";

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <Badge key={item} variant="outline" className="font-mono">
          {item}
        </Badge>
      ))}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </div>
  );
}

// The full role contract for one selected agent (§2.1): authority, memory scopes,
// tool grants, KPIs, and any failure-policy override.
export function AgentDetail({ agent }: { agent: AgentDefinition }) {
  const { authority, memory_scopes: scopes, failure_policy: failure } = agent;
  return (
    <div className="space-y-5 rounded-lg border bg-card p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">{agent.display_name}</h2>
          <p className="font-mono text-xs text-muted-foreground">
            {agent.agent_id} · reports to {agent.supervisor}
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Badge variant="secondary">
            {agent.tier === "cross" ? "cross" : `tier ${agent.tier}`}
          </Badge>
          <Badge variant={MODEL_TIER_VARIANT[agent.model_policy.tier]}>
            {agent.model_policy.tier}
          </Badge>
          {authority.veto ? <Badge variant="destructive">veto</Badge> : null}
        </div>
      </div>

      <Section title="Decides alone">
        <ul className="space-y-1 text-sm">
          {authority.decides_alone.map((item) => (
            <li key={item} className="flex gap-2">
              <span aria-hidden className="text-muted-foreground">
                ·
              </span>
              <span>{item}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Must escalate">
        {authority.must_escalate.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Terminal-advisory — nothing to escalate.
          </p>
        ) : (
          <ul className="space-y-2 text-sm">
            {authority.must_escalate.map((esc) => (
              <li
                key={esc.decision}
                className="flex flex-wrap items-center gap-x-2 gap-y-1"
              >
                <span>{esc.decision}</span>
                <span aria-hidden className="text-muted-foreground">
                  →
                </span>
                {esc.to.map((target) => (
                  <Badge key={target} variant="outline" className="font-mono">
                    {target}
                  </Badge>
                ))}
                {esc.co_sign ? (
                  <Badge variant="secondary">co-sign</Badge>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <div className="grid gap-5 sm:grid-cols-2">
        <Section title="Reads">
          <Chips items={scopes.read} />
        </Section>
        <Section title="Writes">
          <Chips items={scopes.write} />
        </Section>
      </div>

      <Section title="Tool grants">
        <Chips items={agent.tool_grants} />
      </Section>

      <Section title="KPIs">
        <Chips items={agent.kpis} />
      </Section>

      <Section title="Failure policy">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="outline">{failure.mode}</Badge>
          {failure.escalates_to_human ? (
            <Badge variant="outline">escalates to human</Badge>
          ) : null}
          {failure.watchdog_restart ? (
            <Badge variant="outline">watchdog restart</Badge>
          ) : null}
          {failure.skip_retries_on.map((item) => (
            <Badge key={item} variant="outline" className="font-mono">
              skip-retry: {item}
            </Badge>
          ))}
        </div>
        {failure.notes ? (
          <p className="mt-2 text-xs text-muted-foreground">{failure.notes}</p>
        ) : null}
      </Section>
    </div>
  );
}
