import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { CanvasSkeleton } from "./canvas";
import { ArchitectureEmpty, BoardEmpty, BoardError } from "./graph-states";
import { ListView } from "./list-view";
import type { GraphModel } from "@/lib/graph/model";

/**
 * The graph screens' four states (§6.4/§6.8).
 *
 * The CANVAS is deliberately absent from these stories. React Flow needs
 * ResizeObserver and real layout measurement; Storybook's Vite builder renders in
 * a real browser so it would work there, but the same component cannot be
 * exercised in the jsdom test suite — so the canvas is proven by neither. What IS
 * storied is every state that does not need it, plus the List view, which is the
 * accessible alternative (TITAN-FRT-047) and the one surface the tests do cover.
 */
const meta: Meta = { title: "Graph/States" };
export default meta;

const MODEL: GraphModel<unknown> = {
  nodes: [
    { id: "a", kind: "task", label: "Schema DB", data: {} },
    { id: "b", kind: "task", label: "Auth API", data: {} },
    { id: "c", kind: "task", label: "Payment API", data: {} },
    { id: "d", kind: "task", label: "Checkout UI", data: {} },
  ],
  edges: [
    { from: "a", to: "b" },
    { from: "b", to: "c", blocked: true },
    { from: "c", to: "d" },
  ],
};

/** Loading: a skeleton canvas that says what it is waiting on. */
export const Loading: StoryObj = { render: () => <CanvasSkeleton /> };

/** Empty: §6.4 says "same as board empty". */
export const Empty: StoryObj = { render: () => <BoardEmpty /> };

/** Empty, architecture: it is a pipeline OUTPUT, so name the stage. */
export const ArchitectureNotDesigned: StoryObj = {
  render: () => <ArchitectureEmpty projectId="p1" />,
};

/** Error: a retry panel. */
export const Error: StoryObj = {
  render: () => <BoardError onRetry={() => undefined} />,
};

/** Ready, as the List view (TITAN-FRT-047) — the same nodes, edges and actions. */
export const ListViewReady: StoryObj = {
  render: () => (
    <ListView
      model={MODEL}
      selectedId="b"
      onSelect={() => undefined}
      emptyLabel="No tasks yet"
    />
  ),
};
