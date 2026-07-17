"use client";

/**
 * The frame both graph screens share: toolbar, the canvas/List-view toggle
 * (TITAN-FRT-047), the clustering guard, and the four states.
 *
 * Layout runs HERE rather than inside the canvas, because the canvas is
 * dynamically imported and layout is a pure function — computing it outside
 * keeps it testable and keeps the decision of full-vs-incremental in one place.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, List, Network, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { GraphCanvas } from "@/components/graph/canvas";
import { exportSvg } from "@/lib/graph/export";
import { ListView } from "@/components/graph/list-view";
import { Button } from "@/components/ui/button";
import {
  cluster,
  incrementalLayout,
  layout,
  needsClustering,
  type ClusterData,
  type Direction,
} from "@/lib/graph/layout";
import type { GraphModel, GraphNode, Positioned } from "@/lib/graph/model";

export interface GraphShellProps<D> {
  model: GraphModel<D>;
  selectedId: string | null;
  onSelect: (id: string | null, additive: boolean) => void;
  onActivate?: (id: string) => void;
  accented?: ReadonlySet<string>;
  spotlight?: ReadonlySet<string> | null;
  alsoSelected?: ReadonlySet<string>;
  direction?: Direction;
  /** Groups nodes when the graph trips ADR-FRT-04's 500-node guard. */
  groupOf: (node: GraphNode<D>) => string;
  /** Rendered in the toolbar, before the view toggle. */
  toolbar?: React.ReactNode;
  /** Rendered under the canvas — §6.4's selection inspector bar. */
  inspector?: React.ReactNode;
  emptyLabel: string;
  /** When set, offers an SVG export of the CURRENT view under this filename. */
  exportName?: string;
}

export function GraphShell<D>({
  model,
  selectedId,
  onSelect,
  onActivate,
  accented,
  spotlight,
  alsoSelected,
  direction = "LR",
  groupOf,
  toolbar,
  inspector,
  emptyLabel,
  exportName,
}: GraphShellProps<D>) {
  const [view, setView] = useState<"canvas" | "list">("canvas");
  const [drilled, setDrilled] = useState<string | null>(null);
  const [relayoutNonce, setRelayoutNonce] = useState(0);

  // Past the guard, collapse to one node per group and let a double-click open
  // one (ADR-FRT-04: React Flow renders a DOM node each, which stops being
  // interactive somewhere past ~500).
  const clustered = needsClustering(model) && drilled === null;
  const shown: GraphModel<unknown> = useMemo(() => {
    if (clustered) {
      return cluster(model, groupOf) as GraphModel<unknown>;
    }
    if (drilled !== null) {
      const inside = model.nodes.filter((n) => groupOf(n) === drilled);
      const ids = new Set(inside.map((n) => n.id));
      return {
        nodes: inside,
        edges: model.edges.filter((e) => ids.has(e.from) && ids.has(e.to)),
      } as GraphModel<unknown>;
    }
    return model as GraphModel<unknown>;
  }, [model, clustered, drilled, groupOf]);

  // Hold positions across renders so a realtime patch recolours in place instead
  // of re-ranking the graph under the reader (§6.4). A full pass happens on
  // mount, on an explicit Re-layout, and whenever the view's SHAPE changes —
  // where holding stale positions would be nonsense rather than kind.
  //
  // "Shape" is NOT just clustering/drill/direction. The caller can hand us a
  // model that is a different graph entirely: the architecture screen swaps the
  // version, filters a layer, or toggles the diff. Those share no node identity
  // with what came before, so taking the incremental path would hold coordinates
  // computed for a graph that no longer exists — nodes stranded at another
  // version's positions, and anything new placed by the fallback heuristic
  // instead of dagre. The node-id set is the honest identity of "the same
  // graph, changed" vs "a different graph".
  const held = useRef<readonly Positioned<unknown>[]>([]);
  const shapeKey = `${clustered ? "cluster" : (drilled ?? "full")}:${direction}:${relayoutNonce}`;
  const lastShape = useRef(shapeKey);
  const laid = useMemo(() => {
    // Same graph plus-or-minus nodes = incremental. A DISJOINT node set — nothing
    // in common with what we hold — means the caller REPLACED the graph, so lay
    // it out afresh.
    const heldIds = new Set(held.current.map((n) => n.id));
    const overlaps = shown.nodes.some((n) => heldIds.has(n.id));
    const reset =
      lastShape.current !== shapeKey || (held.current.length > 0 && !overlaps);
    lastShape.current = shapeKey;
    const next =
      reset || held.current.length === 0
        ? layout(shown, { direction })
        : incrementalLayout(shown, held.current, { direction });
    held.current = next.nodes;
    return next;
  }, [shown, direction, shapeKey]);

  const onCanvasActivate = useCallback(
    (id: string) => {
      if (clustered) {
        setDrilled(id); // a cluster's id IS its group
        return;
      }
      onActivate?.(id);
    },
    [clustered, onActivate],
  );

  // A drill-down that no longer matches any group (the graph shrank under us)
  // would render an empty canvas with no way back — fall out of it.
  useEffect(() => {
    if (drilled !== null && !model.nodes.some((n) => groupOf(n) === drilled)) {
      setDrilled(null);
    }
  }, [drilled, model, groupOf]);

  // While clustered, the rendered ids are GROUP keys — `accented`/`spotlight`
  // hold the caller's node ids (tasks), so nothing would ever match and every
  // cluster would render muted and un-accented. Drop them for that view rather
  // than dim the whole board.
  const shownAccent = clustered ? undefined : accented;
  const shownSpotlight = clustered ? null : spotlight;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {toolbar}
        {drilled !== null ? (
          <Button variant="outline" size="sm" onClick={() => setDrilled(null)}>
            ← All groups
          </Button>
        ) : null}
        {clustered ? (
          <span className="text-xs text-muted-foreground">
            {model.nodes.length} nodes — grouped for performance. Open a group
            to see its nodes.
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRelayoutNonce((n) => n + 1)}
            title="Recompute the whole layout"
          >
            <RefreshCw aria-hidden className="mr-1 size-3" />
            Re-layout
          </Button>
          {/* TITAN-FRT-047: the same nodes, edges and selection, as a table. */}
          <Button
            variant={view === "list" ? "default" : "outline"}
            size="sm"
            aria-pressed={view === "list"}
            onClick={() => setView(view === "list" ? "canvas" : "list")}
          >
            {view === "list" ? (
              <Network aria-hidden className="mr-1 size-3" />
            ) : (
              <List aria-hidden className="mr-1 size-3" />
            )}
            {view === "list" ? "Canvas" : "List view"}
          </Button>
          {exportName ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const result = exportSvg(laid, exportName);
                if (!result.ok) {
                  // Never a silent no-op: a button that does nothing is
                  // indistinguishable from a broken one.
                  toast.error(`Couldn’t export: ${result.reason}.`);
                }
              }}
              title="Download this view as an SVG (client-side, offline-safe)"
            >
              <Download aria-hidden className="mr-1 size-3" />
              SVG
            </Button>
          ) : null}
        </div>
      </div>

      {view === "list" ? (
        <ListView
          model={shown}
          selectedId={selectedId}
          onSelect={(id) => onSelect(id, false)}
          // TITAN-FRT-047 says the list exposes the same ACTIONS. Past the
          // clustering guard the canvas's action is "open this group" — without
          // it the list would strand a keyboard user on cluster rows with no way
          // to reach a real node.
          onActivate={onCanvasActivate}
          activateLabel={clustered ? "Open group" : undefined}
          emptyLabel={emptyLabel}
        />
      ) : shown.nodes.length === 0 ? (
        <p className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          {emptyLabel}
        </p>
      ) : (
        <GraphCanvas
          laid={laid}
          selectedId={selectedId}
          accented={shownAccent}
          spotlight={shownSpotlight}
          alsoSelected={alsoSelected}
          onSelect={onSelect}
          onActivate={onCanvasActivate}
        />
      )}

      {inspector}
    </div>
  );
}

export type { ClusterData };
