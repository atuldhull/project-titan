/**
 * The accessible alternative to the canvas (TITAN-FRT-047).
 *
 * This test carries more weight than usual. A React Flow canvas cannot be
 * rendered in jsdom at all — it needs ResizeObserver and real layout, and jsdom
 * has neither — so the canvas is genuinely untested here. The List view is the
 * one surface where "the graph exposes the same nodes, edges and actions" is
 * actually asserted, and it is also the surface a screen-reader user gets.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ListView } from "@/components/graph/list-view";
import type { GraphModel } from "@/lib/graph/model";

const MODEL: GraphModel<unknown> = {
  nodes: [
    { id: "a", kind: "task", label: "Schema DB", data: {} },
    { id: "b", kind: "task", label: "Auth API", data: {} },
    { id: "c", kind: "task", label: "Payments", data: {} },
  ],
  edges: [
    { from: "a", to: "b" },
    { from: "b", to: "c", blocked: true },
  ],
};

describe("ListView", () => {
  it("exposes every node as a row", () => {
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        emptyLabel="none"
      />,
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(4); // header + 3
  });

  /** The row a node OWNS — not every row that merely mentions it. */
  const rowFor = (label: string): HTMLElement =>
    screen.getByRole("button", { name: label }).closest("tr") as HTMLElement;

  it("states each node's dependencies in BOTH directions", () => {
    // On the canvas an edge is geometry — "what does this depend on" is
    // unanswerable without sight. Here it is a column.
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        emptyLabel="none"
      />,
    );
    const cells = within(rowFor("Auth API")).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("Schema DB"); // depends on
    expect(cells[1]).toHaveTextContent("Payments"); // required by
  });

  it("says 'Nothing' rather than leaving a dependency cell blank", () => {
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        emptyLabel="none"
      />,
    );
    expect(
      within(rowFor("Schema DB")).getAllByRole("cell")[0],
    ).toHaveTextContent("Nothing");
  });

  it("selects a node — the same action the canvas offers", () => {
    const onSelect = vi.fn();
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={onSelect}
        emptyLabel="none"
      />,
    );
    return userEvent
      .click(screen.getByRole("button", { name: "Payments" }))
      .then(() => {
        expect(onSelect).toHaveBeenCalledWith("c");
      });
  });

  it("marks the selected row for assistive tech, not just visually", () => {
    render(
      <ListView
        model={MODEL}
        selectedId="b"
        onSelect={vi.fn()}
        emptyLabel="none"
      />,
    );
    expect(screen.getByRole("button", { name: "Auth API" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("offers the activate action with no label supplied — TITAN-FRT-047", async () => {
    // The regression this pins down. The row used to render its activate button
    // only when BOTH `onActivate` and `activateLabel` were set, and the only
    // caller passed a label while CLUSTERED. So on an ordinary (unclustered)
    // task graph the canvas had a double-click that opened the task on the board
    // and the list had no equivalent whatsoever — which is exactly the half of
    // -047 that says the list exposes the same DATA AND ACTIONS, missed on the
    // common path and satisfied only on the rare one.
    //
    // The label is a name for the action, never a switch for it: an action that
    // exists is offered, and falls back to "Open" if nobody named it.
    const user = userEvent.setup();
    const onActivate = vi.fn();
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        onActivate={onActivate}
        emptyLabel="none"
      />,
    );
    await user.click(screen.getAllByRole("button", { name: "Open" })[1]);
    expect(onActivate).toHaveBeenCalledWith("b");
  });

  it("names the activate action when the screen supplies a label", () => {
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        onActivate={vi.fn()}
        activateLabel="Open on the board"
        emptyLabel="none"
      />,
    );
    expect(
      screen.getAllByRole("button", { name: "Open on the board" }),
    ).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Open" })).toBeNull();
  });

  it("offers no activate button when the screen has no such action", () => {
    // A dead "Open" that does nothing is worse than no button: it reads to a
    // screen reader as an available action and then silently is not one.
    render(
      <ListView
        model={MODEL}
        selectedId={null}
        onSelect={vi.fn()}
        activateLabel="Open on the board"
        emptyLabel="none"
      />,
    );
    expect(screen.queryByRole("button", { name: /open/i })).toBeNull();
  });

  it("renders the empty state instead of a bare table", () => {
    render(
      <ListView
        model={{ nodes: [], edges: [] }}
        selectedId={null}
        onSelect={vi.fn()}
        emptyLabel="No tasks yet"
      />,
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("No tasks yet")).toBeInTheDocument();
  });

  it("survives an edge naming a node that is not in the model", () => {
    render(
      <ListView
        model={{ nodes: [MODEL.nodes[0]], edges: [{ from: "a", to: "ghost" }] }}
        selectedId={null}
        onSelect={vi.fn()}
        emptyLabel="none"
      />,
    );
    // The ghost must not become a row, and must not be listed as a dependant.
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + Schema DB
    expect(
      within(rowFor("Schema DB")).getAllByRole("cell")[1],
    ).toHaveTextContent("Nothing");
  });
});
