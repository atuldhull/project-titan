/**
 * Where a LIFTED card goes on an arrow press (Vol 4 §10, TITAN-FRT-025).
 *
 * dnd-kit's default `coordinateGetter` translates the card 25px per press, which
 * on 256px columns is ~11 presses to reach the next one and ~150 to cross this
 * board's 14. "Fully operable by keyboard" is a MUST; that is operable only in
 * the sense that a mountain is climbable.
 */

import { describe, expect, it } from "vitest";

import {
  nextColumnCoordinates,
  type ColumnRect,
} from "@/lib/kanban/navigation";

/** Three 250px columns at x = 0, 260, 520 — centres 125, 385, 645. */
const COLUMNS: ColumnRect[] = [
  { left: 0, top: 0, width: 250, height: 400 },
  { left: 260, top: 0, width: 250, height: 400 },
  { left: 520, top: 0, width: 250, height: 400 },
];

describe("nextColumnCoordinates", () => {
  it("jumps a whole column per press", () => {
    expect(
      nextColumnCoordinates("ArrowRight", { x: 125, y: 100 }, COLUMNS),
    ).toEqual({ x: 385, y: 200 });
  });

  it("goes left to the previous column", () => {
    expect(
      nextColumnCoordinates("ArrowLeft", { x: 385, y: 100 }, COLUMNS),
    ).toEqual({ x: 125, y: 200 });
  });

  it("returns a point INSIDE the target column, not merely near it", () => {
    // dnd-kit resolves the drop by hit-testing the coordinates it is handed; a
    // point outside the target's rect lands nowhere and the move is lost.
    const point = nextColumnCoordinates(
      "ArrowRight",
      { x: 125, y: 100 },
      COLUMNS,
    );
    const target = COLUMNS[1];
    expect(point!.x).toBeGreaterThan(target.left);
    expect(point!.x).toBeLessThan(target.left + target.width);
    expect(point!.y).toBeGreaterThan(target.top);
    expect(point!.y).toBeLessThan(target.top + target.height);
  });

  it("stays put at the board's edges", () => {
    expect(
      nextColumnCoordinates("ArrowLeft", { x: 125, y: 100 }, COLUMNS),
    ).toBeUndefined();
    expect(
      nextColumnCoordinates("ArrowRight", { x: 645, y: 100 }, COLUMNS),
    ).toBeUndefined();
  });

  it("crosses several columns one press at a time", () => {
    let at = { x: 125, y: 100 };
    at = nextColumnCoordinates("ArrowRight", at, COLUMNS)!;
    expect(at.x).toBe(385);
    at = nextColumnCoordinates("ArrowRight", at, COLUMNS)!;
    expect(at.x).toBe(645);
  });

  it("leaves vertical to dnd-kit's default nudge", () => {
    expect(
      nextColumnCoordinates("ArrowDown", { x: 125, y: 100 }, COLUMNS),
    ).toBeUndefined();
    expect(
      nextColumnCoordinates("ArrowUp", { x: 125, y: 100 }, COLUMNS),
    ).toBeUndefined();
  });

  it("does nothing before the columns are measured", () => {
    expect(
      nextColumnCoordinates("ArrowRight", { x: 0, y: 0 }, []),
    ).toBeUndefined();
  });

  it("does not depend on the rects arriving in visual order", () => {
    // droppableRects is a Map keyed by status; its iteration order is whatever
    // dnd-kit registered, not left-to-right.
    const shuffled = [COLUMNS[2], COLUMNS[0], COLUMNS[1]];
    expect(
      nextColumnCoordinates("ArrowRight", { x: 125, y: 100 }, shuffled),
    ).toEqual({ x: 385, y: 200 });
  });
});
