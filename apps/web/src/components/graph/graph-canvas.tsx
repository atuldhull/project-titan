"use client";

/**
 * The shared graph canvas (ADR-FRT-04) — React Flow + the dagre layout from
 * `lib/graph`, with a node-type registry so one engine serves both §6.4 and §6.8.
 *
 * It owns rendering and interaction ONLY. Every decision that could be wrong —
 * where nodes go, which are on the critical path, what changed between two
 * versions, when to cluster — is a pure function in `lib/graph`, tested without a
 * browser. That split is not tidiness: this repo has no way to run a browser, so
 * anything that lives in here is, by construction, untested.
 *
 * This module must stay dynamically imported (TITAN-FRT-005) — see `canvas.tsx`.
 */

import { useCallback, useMemo, useRef } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";

import "@xyflow/react/dist/style.css";

import { NODE_TYPES, type RenderData } from "@/components/graph/node-types";
import {
  degrees,
  edgeAriaLabel,
  labelsById,
  nodeAriaLabel,
} from "@/lib/graph/aria";
import { CANVAS_FRAME_CLASS } from "@/lib/graph/canvas-frame";
import type { LaidOut } from "@/lib/graph/model";
import { useShortcuts } from "@/lib/shortcuts/provider";
import { cn } from "@/lib/utils";

export interface CanvasProps {
  laid: LaidOut<unknown>;
  selectedId: string | null;
  /** Ids to accent (the critical path, or the chain under the cursor). */
  accented?: ReadonlySet<string>;
  /** When set, everything outside it dims (§6.4's hover highlight). */
  spotlight?: ReadonlySet<string> | null;
  onSelect: (id: string | null, additive: boolean) => void;
  onActivate?: (id: string) => void;
  /** Extra ids selected alongside `selectedId` (Shift+click multi-select). */
  alsoSelected?: ReadonlySet<string>;
}

function toNodes(props: CanvasProps): Node<RenderData>[] {
  const { laid, selectedId, accented, spotlight, alsoSelected } = props;
  const { incoming, outgoing } = degrees(laid.edges);
  return laid.nodes.map((node) => ({
    id: node.id,
    type: node.kind in NODE_TYPES ? node.kind : "task",
    position: { x: node.x, y: node.y },
    // React Flow needs the size up front or it measures 0 and the minimap
    // collapses; the layout already decided it.
    width: node.width,
    height: node.height,
    selected: selectedId === node.id || alsoSelected?.has(node.id) === true,
    // TITAN-FRT-043: React Flow's default name for a node is its id, and every
    // id here is a UUID. See `lib/graph/aria.ts` for what is said instead.
    ariaLabel: nodeAriaLabel(
      node.label,
      node.kind,
      incoming.get(node.id) ?? 0,
      outgoing.get(node.id) ?? 0,
    ),
    data: {
      label: node.label,
      kind: node.kind,
      domain: node.data,
      muted: spotlight != null && !spotlight.has(node.id),
      accented: accented?.has(node.id) === true,
      selected: selectedId === node.id || alsoSelected?.has(node.id) === true,
    },
  }));
}

function toEdges(props: CanvasProps): Edge[] {
  const { laid, spotlight, accented } = props;
  // Node ids are UUIDs. React Flow's default edge name is "Edge from <id> to
  // <id>", so without this every edge reads as 72 characters of hex —
  // technically labelled, practically silent (TITAN-FRT-043).
  const labels = labelsById(laid.nodes);
  return laid.edges.map((edge) => {
    const onChain =
      spotlight == null || (spotlight.has(edge.from) && spotlight.has(edge.to));
    const onPath =
      accented?.has(edge.from) === true && accented.has(edge.to) === true;
    return {
      id: `${edge.from}->${edge.to}`,
      source: edge.from,
      target: edge.to,
      ariaLabel: edgeAriaLabel(
        labels.get(edge.from) ?? edge.from,
        labels.get(edge.to) ?? edge.to,
        edge.blocked === true,
      ),
      // §6.4: "blocked edges rendered dashed-red".
      animated: false,
      style: {
        strokeDasharray: edge.blocked ? "4 3" : undefined,
        // Semantic CSS variables, not hex — TITAN-FRT-010 bans raw colours.
        stroke: edge.blocked
          ? "var(--destructive)"
          : onPath
            ? "var(--primary)"
            : undefined,
        strokeWidth: onPath ? 2 : undefined,
        opacity: onChain ? 1 : 0.15,
      },
    };
  });
}

/** One arrow press pans this many pixels. */
const PAN_STEP = 80;

/**
 * §10's Graph bindings: `+`/`-` zoom, `0` fit, arrows pan, Tab cycles nodes.
 *
 * They are split across TWO mechanisms, and the split is the whole point of this
 * function, so it is worth being explicit about why.
 *
 * **Zoom and fit go through the global engine** (`useShortcuts`, group "Graph").
 * They are screen-scoped in §10's sense — pressing `0` while focus sits on the
 * version select should still fit the graph — and bare `+`/`-`/`0` carry no
 * browser default worth protecting, so the engine's unconditional
 * `preventDefault` costs nothing. Registering them there is also the only way
 * they appear in the `?` overlay (TITAN-FRT-048), which reads the live table.
 *
 * **Pan and Tab stay a React `onKeyDown` on the canvas element.** This is NOT
 * laziness about migrating, it is forced: the engine calls `preventDefault()` on
 * every match BEFORE it calls `run`, so a binding cannot decline a key it turns
 * out not to want. For `Tab` that would mean the whole page loses tab navigation
 * for as long as a graph is mounted — the focus trap TITAN-FRT-042 forbids, and
 * the exact bug the fix below exists to remove. For the arrows it would mean the
 * ADR record dialog (`adr-panel.tsx`, `max-h-[80vh] overflow-y-auto`) could not
 * be scrolled by keyboard while it is open, because a target inside a Radix
 * dialog is not "behind" the overlay by `isBehindOverlay`'s test and so is not
 * suppressed. Handled here, both keys are consumed only when focus is genuinely
 * inside the canvas and `preventDefault` is called only when we act.
 *
 * The cost, honestly: pan and Tab do not appear in the `?` overlay. The canvas's
 * own `aria-label` states them. Lifting them into the table needs a
 * listing-without-dispatch registration in `lib/shortcuts` — see the report.
 */
function useCanvasKeys({
  laid,
  selectedId,
  onSelect,
}: {
  laid: LaidOut<unknown>;
  selectedId: string | null;
  onSelect: (id: string | null, additive: boolean) => void;
}): React.KeyboardEventHandler<HTMLDivElement> {
  const flow = useReactFlow();
  const order = useMemo(() => laid.nodes.map((n) => n.id), [laid.nodes]);
  // Refs, not deps: the handler must see the CURRENT layout and selection
  // without being rebuilt every time a realtime patch re-ranks the graph.
  const orderRef = useRef(order);
  orderRef.current = order;
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;

  useShortcuts([
    {
      id: "graph-zoom-in",
      keys: "+",
      label: "Zoom in",
      group: "Graph",
      run: () => flow.zoomIn(),
    },
    {
      // The same physical key unshifted. Kept because the original binding
      // accepted it and dropping it would silently break every user who never
      // reached for Shift.
      id: "graph-zoom-in-unshifted",
      keys: "=",
      label: "Zoom in (without Shift)",
      group: "Graph",
      run: () => flow.zoomIn(),
    },
    {
      id: "graph-zoom-out",
      keys: "-",
      label: "Zoom out",
      group: "Graph",
      run: () => flow.zoomOut(),
    },
    {
      id: "graph-fit",
      keys: "0",
      label: "Fit the whole graph in view",
      group: "Graph",
      run: () => void flow.fitView(),
    },
  ]);

  return useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>): void => {
      const target = event.target;
      // §10: shortcuts are disabled inside text inputs. The canvas has none of
      // its own, but React Flow's node renderers are arbitrary components and
      // the architecture screen's controls sit one container away.
      if (
        target instanceof HTMLElement &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      const pan = (x: number, y: number): void => {
        const at = flow.getViewport();
        flow.setViewport({ ...at, x: at.x + x, y: at.y + y });
      };
      switch (event.key) {
        case "ArrowLeft":
          event.preventDefault();
          pan(PAN_STEP, 0);
          return;
        case "ArrowRight":
          event.preventDefault();
          pan(-PAN_STEP, 0);
          return;
        case "ArrowUp":
          event.preventDefault();
          pan(0, PAN_STEP);
          return;
        case "ArrowDown":
          event.preventDefault();
          pan(0, -PAN_STEP);
          return;
        case "Tab": {
          const ids = orderRef.current;
          const at = ids.indexOf(selectedRef.current ?? "");
          // TITAN-FRT-042, and this branch is the whole reason it is a bug fix
          // rather than a refactor.
          //
          // `at === -1` is the state the canvas MOUNTS in — nothing selected —
          // and also what a selection filtered out of the current view leaves
          // behind. The previous version treated -1 as "one before the first"
          // and so answered the very first Tab by selecting node 0 and eating
          // the key. A user who was merely tabbing THROUGH the page towards the
          // ADR panel was captured, and getting out again cost one press per
          // node: on a 300-node graph that is a trap in every sense that
          // matters, whatever the arithmetic says about the last index.
          //
          // So: Tab cycles only once a node is already selected, and releases at
          // both ends. Entering the cycle is a selection — a click, the List
          // view (FRT-047), or a `?node=` deep link — not a keystroke that a
          // passer-by can trigger by accident.
          if (at === -1) {
            return;
          }
          const next = at + (event.shiftKey ? -1 : 1);
          if (next < 0 || next >= ids.length) {
            return; // at an end — let the browser move focus out of the canvas
          }
          event.preventDefault();
          onSelect(ids[next], false);
          return;
        }
        default:
      }
    },
    [flow, onSelect],
  );
}

function Canvas(props: CanvasProps) {
  const nodes = useMemo(() => toNodes(props), [props]);
  const edges = useMemo(() => toEdges(props), [props]);
  const { onSelect, onActivate } = props;

  const onKeyDown = useCanvasKeys({
    laid: props.laid,
    selectedId: props.selectedId,
    onSelect,
  });

  const onNodeClick = useCallback(
    (event: React.MouseEvent, node: Node) => {
      onSelect(node.id, event.shiftKey);
    },
    [onSelect],
  );

  return (
    <div
      // Focusable so pan and Tab have somewhere to be scoped TO, and labelled so
      // a screen reader announces what it just entered rather than a silent pile
      // of divs. The label carries the two bindings that cannot be listed in the
      // `?` overlay — see `useCanvasKeys`.
      tabIndex={0}
      role="application"
      onKeyDown={onKeyDown}
      aria-label="Graph canvas. Plus and minus zoom, zero fits, arrows pan, and tab steps through nodes once one is selected. Use the List view button for a table of the same data."
      className={cn(
        CANVAS_FRAME_CLASS,
        "outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        minZoom={0.1}
        // Read-only: dependencies are edited by agents, humans request changes
        // through the task Sheet (§6.4). Anything that implied a draggable graph
        // would promise an edit with no endpoint.
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={(_, node) => onActivate?.(node.id)}
        onPaneClick={() => onSelect(null, false)}
        proOptions={{ hideAttribution: false }}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable ariaLabel="Graph mini-map" />
      </ReactFlow>
    </div>
  );
}

/** The canvas, with the React Flow context it needs for the keybindings. */
export function GraphCanvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

export default GraphCanvas;
