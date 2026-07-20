"use client";

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { StateChip } from "@/components/agents/state-chip";
import type { AgentView } from "@/lib/agents/view";
import { MODEL_TIER_VARIANT } from "@/lib/agents/types";
import { cn } from "@/lib/utils";

/** "2m 10s" — §6.5's elapsed readout. */
export function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

/** "640 tok/s" / "1.2k tok/s". */
export function formatThroughput(tps: number): string {
  return tps >= 1000
    ? `${(tps / 1000).toFixed(1)}k tok/s`
    : `${Math.round(tps)} tok/s`;
}

/**
 * One agent tile: identity, state, current work, the model really in use.
 *
 * The whole card is a link to the agent's drawer rather than a button, so
 * middle-click and "open in new tab" behave — the drawer is a real route.
 */
export function AgentCard({ view }: { view: AgentView }) {
  const { agent, definition, state, model, throughput, elapsedMs, failure } =
    view;
  // From the RUN's project, not the agent's: an org-level agent has no project
  // of its own and would lose its task link on exactly the cards that are
  // working.
  const taskHref =
    view.currentTaskId && view.runProjectId
      ? `/projects/${view.runProjectId}/board?task=${view.currentTaskId}`
      : null;

  return (
    <div
      className={cn(
        "relative flex flex-col gap-2 rounded-lg border bg-card p-4 transition-colors",
        "focus-within:ring-2 focus-within:ring-ring/50 hover:border-ring/60",
        state === "error" && "border-destructive/60",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          {/* The card's own link. `after:absolute after:inset-0` makes the whole
              tile clickable while keeping ONE control in the a11y tree.
              The cost: that pseudo-element is the hit target for everything
              beneath it, so anything with a `title` — or its own link — must be
              lifted above it with `relative z-10`, or its tooltip is
              unreachable and the click goes to the card instead. */}
          <Link
            href={`/agents/${agent.id}`}
            className="truncate text-sm font-medium after:absolute after:inset-0 focus-visible:outline-none"
          >
            {agent.name}
          </Link>
          <p className="truncate font-mono text-xs text-muted-foreground">
            {agent.role}
          </p>
        </div>
        <StateChip state={state} />
      </div>

      {/* Current work. §6.5 wants a task link; it comes from the agent's RUN,
          because agents.current_task_id has no writer and is always null. */}
      {failure ? (
        <p
          className="relative z-10 truncate text-xs text-destructive"
          title={failure.error ?? undefined}
        >
          {failure.error ?? "run failed"}
        </p>
      ) : taskHref ? (
        // Sits above the card-wide overlay so it is separately clickable.
        <Link
          href={taskHref}
          className="relative z-10 truncate text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          {view.currentTaskId?.slice(0, 8)}
        </Link>
      ) : (
        <p className="text-xs text-muted-foreground">
          {state === "paused" ? "paused by an operator" : "no active task"}
        </p>
      )}

      {/* z-10: these badges carry the title tooltips that explain configured-vs-
          actual and what the throughput is averaged over. Under the overlay they
          would never appear. */}
      <div className="relative z-10 flex flex-wrap items-center gap-1.5">
        {model.model ? (
          <Badge
            variant="outline"
            // §8/TITAN-FRT-041: no horizontal body scroll at 375px. This is the
            // one unbounded string on the card — a fully-qualified binding like
            // `anthropic/claude-sonnet-4-5-20250929 (configured)` is ~45 mono
            // chars, wider than the 311px content box of a 1-column card, and a
            // Badge is `inline-flex` with no wrapping, so it would push the grid
            // out. `max-w-full` is what bounds it: `min-w-0` cannot, because
            // `badgeVariants` also sets `shrink-0`, so this item never shrinks
            // below its content no matter what its min-width says.
            className="max-w-full font-mono"
            // §6.5 wants "model+provider IN USE". When nothing has run, all we
            // have is the CONFIGURED binding, and the two really can differ —
            // so the card says which one it is showing rather than implying.
            title={
              model.actual
                ? "The model this agent's most recent run actually used"
                : "This agent's configured binding — it has not run yet"
            }
          >
            {/* The ellipsis has to live on an inner block box. `text-overflow`
                applies to BLOCK CONTAINERS, and a Badge is `inline-flex`, so
                `truncate` on the Badge itself clips the binding mid-character
                with no "…" to say it did. This span is a (blockified) flex
                item, so it both shrinks and renders the ellipsis. */}
            <span className="truncate">
              {model.provider ? `${model.provider}/` : ""}
              {model.model}
              {model.actual ? "" : " (configured)"}
            </span>
          </Badge>
        ) : null}
        {throughput ? (
          <Badge
            variant="secondary"
            title="Average over this agent's last completed run"
          >
            {formatThroughput(throughput.tokensPerSecond)}
          </Badge>
        ) : null}
        {elapsedMs !== null ? (
          <Badge variant="secondary" title="Elapsed on the current run">
            {formatElapsed(elapsedMs)}
          </Badge>
        ) : null}
        {definition?.authority.veto ? (
          <Badge variant="destructive">veto</Badge>
        ) : null}
        {definition ? (
          <Badge variant={MODEL_TIER_VARIANT[definition.model_policy.tier]}>
            {definition.model_policy.tier}
          </Badge>
        ) : null}
      </div>
    </div>
  );
}
