/**
 * Arrow navigation between cards (Vol 4 §10, TITAN-FRT-042).
 */

import { describe, expect, it } from "vitest";

import type { BoardColumnView, Card } from "@/lib/kanban/board-store";
import {
  firstCardId,
  isNavigationKey,
  nextCardId,
} from "@/lib/kanban/navigation";

function card(id: string): Card {
  return {
    id,
    title: `Task ${id}`,
    status: "backlog",
    priority: 3,
    estimate_points: null,
    assignee_agent_id: null,
    sprint_id: null,
  };
}

/** backlog: a,b,c | ready: (empty) | assigned: d | done: e,f */
const COLUMNS: BoardColumnView[] = [
  { status: "backlog", cards: [card("a"), card("b"), card("c")] },
  { status: "ready", cards: [] },
  { status: "assigned", cards: [card("d")] },
  { status: "done", cards: [card("e"), card("f")] },
];

describe("nextCardId", () => {
  it("walks down and up a column", () => {
    expect(nextCardId(COLUMNS, "a", "ArrowDown")).toBe("b");
    expect(nextCardId(COLUMNS, "b", "ArrowUp")).toBe("a");
  });

  it("stops at a column's ends rather than wrapping", () => {
    // Wrapping in a 40-task column silently teleports you to the other end.
    expect(nextCardId(COLUMNS, "a", "ArrowUp")).toBeNull();
    expect(nextCardId(COLUMNS, "c", "ArrowDown")).toBeNull();
  });

  it("skips empty columns when crossing the board", () => {
    // The board has 14 columns and most are usually empty; landing focus on one
    // would strand it with nothing to act on.
    expect(nextCardId(COLUMNS, "a", "ArrowRight")).toBe("d");
    expect(nextCardId(COLUMNS, "d", "ArrowLeft")).toBe("a");
  });

  it("holds the row across columns where it can", () => {
    expect(nextCardId(COLUMNS, "b", "ArrowRight")).toBe("d"); // clamped: 1 card
    expect(nextCardId(COLUMNS, "f", "ArrowLeft")).toBe("d");
  });

  it("clamps to the last card of a shorter column", () => {
    expect(nextCardId(COLUMNS, "c", "ArrowRight")).toBe("d");
  });

  it("stays put at the board's edges", () => {
    expect(nextCardId(COLUMNS, "a", "ArrowLeft")).toBeNull();
    expect(nextCardId(COLUMNS, "f", "ArrowRight")).toBeNull();
  });

  it("ignores keys it does not own", () => {
    expect(nextCardId(COLUMNS, "a", "Enter")).toBeNull();
    expect(nextCardId(COLUMNS, "a", "Space")).toBeNull();
  });

  it("returns null for a card that is not on the board", () => {
    expect(nextCardId(COLUMNS, "gone", "ArrowDown")).toBeNull();
  });
});

describe("firstCardId", () => {
  it("finds the first card, skipping empty columns", () => {
    expect(firstCardId(COLUMNS)).toBe("a");
    expect(firstCardId([{ status: "backlog", cards: [] }, COLUMNS[2]])).toBe(
      "d",
    );
  });

  it("is null on an empty board", () => {
    expect(firstCardId([{ status: "backlog", cards: [] }])).toBeNull();
  });
});

describe("isNavigationKey", () => {
  it("claims only the arrows", () => {
    expect(isNavigationKey("ArrowUp")).toBe(true);
    expect(isNavigationKey("ArrowRight")).toBe(true);
    // Enter and Space belong to opening and lifting (§10) — claiming them here
    // would swallow both.
    expect(isNavigationKey("Enter")).toBe(false);
    expect(isNavigationKey("Space")).toBe(false);
    expect(isNavigationKey("Escape")).toBe(false);
  });
});
