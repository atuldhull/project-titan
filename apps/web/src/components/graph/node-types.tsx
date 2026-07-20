"use client";

/**
 * The node-type registry (ADR-FRT-04: "custom node types per domain").
 *
 * The engine never knows what a task or a component IS — it looks a renderer up
 * by `kind` and hands it the node's opaque `data`. Adding a domain means adding
 * an entry here, not touching the canvas.
 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { CircleAlert, CircleCheck, CircleDot, CircleSlash } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { ComponentNodeData } from "@/lib/graph/architecture";
import type { TaskNodeData } from "@/lib/graph/task-graph";
import type { ClusterData } from "@/lib/graph/layout";
import { cn } from "@/lib/utils";

/** What the engine puts on every React Flow node. */
export interface RenderData extends Record<string, unknown> {
  readonly label: string;
  readonly kind: string;
  readonly domain: unknown;
  /** Dimmed: something else is highlighted and this is not part of it. */
  readonly muted?: boolean;
  /** On the highlighted chain, or on the critical path. */
  readonly accented?: boolean;
  readonly selected?: boolean;
}

/** Status → icon. The vocabulary is the real 14-state machine (Phase 41). */
function StatusIcon({ status }: { status: string }) {
  if (status === "done" || status === "merged") {
    return <CircleCheck aria-hidden className="size-3.5 text-success" />;
  }
  if (status === "blocked" || status === "failed" || status === "escalated") {
    return <CircleAlert aria-hidden className="size-3.5 text-destructive" />;
  }
  if (status === "cancelled") {
    return (
      <CircleSlash aria-hidden className="size-3.5 text-muted-foreground" />
    );
  }
  return <CircleDot aria-hidden className="size-3.5 text-muted-foreground" />;
}

function Shell({
  children,
  data,
  className,
}: {
  children: React.ReactNode;
  data: RenderData;
  className?: string;
}) {
  return (
    <>
      {/* Handles are how React Flow anchors edges; hidden because the graph is
          read-only — dependencies are edited by agents (§6.4), so a handle a
          user could pull would promise an edit that has no endpoint. */}
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div
        className={cn(
          "w-[180px] rounded-md border bg-card px-2 py-1.5 text-xs shadow-xs transition-opacity",
          // TITAN-FRT-044: dimming is an opacity change, which survives neither
          // a grayscale audit nor a screen reader — on its own it says nothing
          // at all. The border turns DOTTED as well (a shape difference, not a
          // colour one, and distinct from the dashed border `ClusterNode` uses
          // for "this is a collapsed group"), and the state is stated in words
          // for anyone not reading pixels. `sr-only` rather than a visible glyph
          // because when a spotlight is on, MOST nodes are muted: a badge on
          // each of 300 boxes would bury the handful that are not.
          data.muted && "border-dotted opacity-25",
          data.accented && "ring-2 ring-primary",
          data.selected && "border-primary",
          className,
        )}
      >
        {data.muted ? (
          <span className="sr-only">
            Not connected to the current selection.
          </span>
        ) : null}
        {children}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  );
}

/** §6.4's task node: status icon, id, title, agent avatar. */
export function TaskNode({ data }: NodeProps) {
  const render = data as unknown as RenderData;
  const task = render.domain as TaskNodeData;
  return (
    <Shell data={render}>
      <div className="flex items-center gap-1">
        <StatusIcon status={task.status} />
        <span className="truncate font-medium">{render.label}</span>
      </div>
      <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground">
        {/* There was a `<span className="font-mono">{String(kind === "task" ?
            "" : "")}</span>` here — an empty string either way, rendering an
            empty element. Removed; the node's id has never been shown on the
            canvas (it is a UUID, see `shortTaskId`) and the inspector bar and
            List view both carry it. */}
        <span>{task.status.replace(/_/g, " ")}</span>
        {task.blockedBy > 0 ? (
          <Badge
            variant="outline"
            className="ml-auto border-warning px-1 py-0 text-[9px] text-warning"
          >
            {task.blockedBy} blocked
          </Badge>
        ) : null}
      </div>
    </Shell>
  );
}

/**
 * §6.8's component node.
 *
 * Its badge is the Clean Architecture LAYER — the artifact's real vocabulary
 * (`entities | use_cases | interface_adapters | frameworks`). §6.8 asks for C4
 * types; see `lib/graph/architecture.ts` for why they cannot be rendered.
 *
 * In diff mode the outline encodes the change: added=green, removed=red ghost,
 * changed=amber (TITAN-FRT-031). The colours are semantic tokens — TITAN-FRT-010
 * bans raw hex.
 */
export function ComponentNode({ data }: NodeProps) {
  const render = data as unknown as RenderData;
  const component = render.domain as ComponentNodeData;
  return (
    <Shell
      data={render}
      className={cn(
        component.diff === "added" && "border-success ring-1 ring-success",
        component.diff === "changed" && "border-warning ring-1 ring-warning",
        component.diff === "removed" &&
          "border-dashed border-destructive opacity-50",
      )}
    >
      <div className="truncate font-medium">{render.label}</div>
      <div className="mt-0.5 flex items-center gap-1">
        <Badge variant="secondary" className="px-1 py-0 text-[9px]">
          {component.layer.replace(/_/g, " ") || "unclassified"}
        </Badge>
        {component.diff ? (
          <span className="ml-auto text-[9px] uppercase text-muted-foreground">
            {component.diff}
          </span>
        ) : null}
      </div>
    </Shell>
  );
}

/** A collapsed group, past ADR-FRT-04's 500-node guard. Says what it hides. */
export function ClusterNode({ data }: NodeProps) {
  const render = data as unknown as RenderData;
  const group = render.domain as ClusterData;
  return (
    <Shell data={render} className="border-dashed">
      <div className="truncate font-medium">{render.label}</div>
      <div className="mt-0.5 text-[10px] text-muted-foreground">
        {group.size} {group.size === 1 ? "node" : "nodes"} · double-click to
        open
      </div>
    </Shell>
  );
}

/** The registry the canvas resolves `kind` through. */
export const NODE_TYPES = {
  task: TaskNode,
  component: ComponentNode,
  cluster: ClusterNode,
};
