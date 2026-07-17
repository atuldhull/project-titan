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
