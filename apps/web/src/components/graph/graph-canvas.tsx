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

import { useCallback, useEffect, useMemo, useRef } from "react";
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
import type { LaidOut } from "@/lib/graph/model";

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
  return laid.nodes.map((node) => ({
    id: node.id,
    type: node.kind in NODE_TYPES ? node.kind : "task",
    position: { x: node.x, y: node.y },
    // React Flow needs the size up front or it measures 0 and the minimap
    // collapses; the layout already decided it.
    width: node.width,
    height: node.height,
    selected: selectedId === node.id || alsoSelected?.has(node.id) === true,
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
  return laid.edges.map((edge) => {
    const onChain =
      spotlight == null || (spotlight.has(edge.from) && spotlight.has(edge.to));
    const onPath =
      accented?.has(edge.from) === true && accented.has(edge.to) === true;
    return {
      id: `${edge.from}->${edge.to}`,
      source: edge.from,
      target: edge.to,
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

/**
 * §10's Graph bindings: `+`/`-` zoom, `0` fit, arrows pan, Tab cycles nodes.
 *
 * SCOPED TO THE CANVAS, not the window. §10 calls these "screen-scoped", and for
 * `Tab` that is not a nicety: this binding preventDefaults it, so a window-level
 * listener would swallow Tab for the whole page while the graph is mounted and
 * strand the keyboard on the toolbar, the version selects and the ADR panel —
 * exactly the trap TITAN-FRT-042 forbids. So the handler returns unless focus is
 * genuinely inside the canvas, which is why the canvas is focusable.
 */
function useKeybindings({
  laid,
  selectedId,
  onSelect,
  container,
}: {
  laid: LaidOut<unknown>;
  selectedId: string | null;
  onSelect: (id: string | null, additive: boolean) => void;
  container: React.RefObject<HTMLDivElement | null>;
}) {
  const flow = useReactFlow();
  const order = useMemo(() => laid.nodes.map((n) => n.id), [laid.nodes]);
  const orderRef = useRef(order);
  orderRef.current = order;
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      // Ours ONLY while the canvas holds focus. Everywhere else — including the
      // toolbar directly above it — the browser's own keyboard must survive.
      const root = container.current;
      if (!root || !target || !root.contains(target)) {
        return;
      }
      // §10: shortcuts are disabled inside text inputs.
      if (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.tagName === "SELECT" ||
        target.isContentEditable
      ) {
        return;
      }
      const pan = (x: number, y: number): void => {
        const at = flow.getViewport();
        flow.setViewport({ ...at, x: at.x + x, y: at.y + y });
      };
      switch (event.key) {
        case "+":
        case "=":
          event.preventDefault();
          flow.zoomIn();
          return;
        case "-":
          event.preventDefault();
          flow.zoomOut();
          return;
        case "0":
          event.preventDefault();
          void flow.fitView();
          return;
        case "ArrowLeft":
          event.preventDefault();
          pan(80, 0);
          return;
        case "ArrowRight":
          event.preventDefault();
          pan(-80, 0);
          return;
        case "ArrowUp":
          event.preventDefault();
          pan(0, 80);
          return;
        case "ArrowDown":
          event.preventDefault();
          pan(0, -80);
          return;
        case "Tab": {
          const ids = orderRef.current;
          if (ids.length === 0) {
            return;
          }
          // Tab CYCLES nodes while the canvas has focus (§10) — and Shift+Tab at
          // the first node hands focus BACK to the page rather than wrapping, so
          // the canvas is somewhere you can leave. The List view remains the
          // accessible path through the same data (TITAN-FRT-047).
          const at = ids.indexOf(selectedRef.current ?? "");
          const step = event.shiftKey ? -1 : 1;
          const next = at + step;
          if (next < 0 || next >= ids.length) {
            return; // let the browser move focus out of the canvas
          }
          event.preventDefault();
          onSelect(ids[next], false);
          return;
        }
        default:
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flow, onSelect, container]);
}

function Canvas(props: CanvasProps) {
  const nodes = useMemo(() => toNodes(props), [props]);
  const edges = useMemo(() => toEdges(props), [props]);
  const { onSelect, onActivate } = props;
  const container = useRef<HTMLDivElement | null>(null);

  useKeybindings({
    laid: props.laid,
    selectedId: props.selectedId,
    onSelect,
    container,
  });

  const onNodeClick = useCallback(
    (event: React.MouseEvent, node: Node) => {
      onSelect(node.id, event.shiftKey);
    },
    [onSelect],
  );

  return (
    <div
      ref={container}
      // Focusable so §10's bindings have somewhere to be scoped TO, and labelled
      // so a screen reader announces what it just entered rather than a silent
      // pile of divs.
      tabIndex={0}
      role="application"
      aria-label="Graph canvas. Plus and minus zoom, zero fits, arrows pan, tab cycles nodes. Use the List view button for a table of the same data."
      className="h-[70vh] w-full rounded-lg border outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
