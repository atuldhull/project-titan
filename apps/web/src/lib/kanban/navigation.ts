/**
 * Arrow-key navigation between cards (Vol 4 §10, TITAN-FRT-042).
 *
 * §10's Board bindings are "arrows navigate cards, Enter opens, Space lifts card
 * then arrows move / Space drops / Esc cancels". Two of those belong to dnd-kit's
 * KeyboardSensor (once a card is lifted, it owns the arrows); this owns the other
 * half — moving FOCUS between cards while nothing is lifted.
 *
 * Note the collision this exists alongside: dnd-kit's KeyboardSensor starts a
 * drag on Enter AND Space by default, which would make Enter lift a card instead
 * of opening it. The board narrows `keyboardCodes` to Space so §10's Enter is
 * free to mean "open".
 *
 * Empty columns are skipped. That is not cosmetic: the board has 14 columns and
 * most are usually empty, so a left/right that landed on them would strand focus
 * and make crossing the board a game of counting invisible stops.
 *
 * Pure: a grid + a key in, a card id out.
 */

import type { KeyboardCoordinateGetter } from "@dnd-kit/core";

import type { BoardColumnView } from "@/lib/kanban/board-store";

/** Where a card sits in the visual grid. */
interface Position {
  readonly column: number;
  readonly row: number;
}

function locate(
  columns: readonly BoardColumnView[],
  cardId: string,
): Position | null {
  for (let column = 0; column < columns.length; column += 1) {
    const row = columns[column].cards.findIndex((c) => c.id === cardId);
    if (row !== -1) {
      return { column, row };
    }
  }
  return null;
}

/**
 * The card an arrow key should move focus to, or null to leave focus alone.
 *
 * Up/Down walk the current column and stop at its ends rather than wrapping —
 * wrapping in a column of 40 tasks silently teleports you. Left/Right cross to
 * the nearest column that HAS cards, holding your row where possible and
 * clamping to that column's last card when it is shorter.
 */
export function nextCardId(
  columns: readonly BoardColumnView[],
  currentId: string,
  key: string,
): string | null {
  const at = locate(columns, currentId);
  if (!at) {
    return null;
  }
  const cards = columns[at.column].cards;

  if (key === "ArrowUp") {
    return at.row > 0 ? cards[at.row - 1].id : null;
  }
  if (key === "ArrowDown") {
    return at.row < cards.length - 1 ? cards[at.row + 1].id : null;
  }
  if (key !== "ArrowLeft" && key !== "ArrowRight") {
    return null;
  }

  const step = key === "ArrowLeft" ? -1 : 1;
  for (
    let column = at.column + step;
    column >= 0 && column < columns.length;
    column += step
  ) {
    const target = columns[column].cards;
    if (target.length > 0) {
      return target[Math.min(at.row, target.length - 1)].id;
    }
  }
  return null; // no populated column that way — stay put
}

/** The first card on the board, for entering the grid from outside. */
export function firstCardId(
  columns: readonly BoardColumnView[],
): string | null {
  for (const column of columns) {
    if (column.cards.length > 0) {
      return column.cards[0].id;
    }
  }
  return null;
}

/** Whether a key is one this module handles (so the board can preventDefault). */
export function isNavigationKey(key: string): boolean {
  return (
    key === "ArrowUp" ||
    key === "ArrowDown" ||
    key === "ArrowLeft" ||
    key === "ArrowRight"
  );
}

/**
 * Where a LIFTED card should go on an arrow press (dnd-kit's `coordinateGetter`).
 *
 * dnd-kit's default translates the card 25px per press, which on 256px columns
 * is ~11 presses to reach the next one and ~150 to cross this board's 14. That
 * is operable only in the sense that a mountain is climbable. This jumps
 * column-to-column instead: one press, one column.
 *
 * Works off the droppables' measured rects rather than our own model, because
 * dnd-kit resolves the drop by hit-testing the coordinates it is handed — a
 * coordinate that is not inside the target's rect lands nowhere.
 */
export interface ColumnRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

export interface Point {
  readonly x: number;
  readonly y: number;
}

/**
 * The pure core: the centre of the next column in `code`'s direction, or
 * undefined to leave dnd-kit's default handling alone.
 */
export function nextColumnCoordinates(
  code: string,
  current: Point,
  rects: readonly ColumnRect[],
): Point | undefined {
  if (rects.length === 0) {
    return undefined; // not measured yet
  }
  if (code !== "ArrowLeft" && code !== "ArrowRight") {
    // Vertical stays on dnd-kit's default nudge: within a column the cards are
    // a list, and a 25px step reads naturally there.
    return undefined;
  }
  const centres = rects
    .map((rect) => ({
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    }))
    .sort((a, b) => a.x - b.x);

  return code === "ArrowRight"
    ? centres.find((c) => c.x > current.x)
    : [...centres].reverse().find((c) => c.x < current.x);
}

/** The dnd-kit adapter — everything real is in `nextColumnCoordinates`. */
export const boardCoordinateGetter: KeyboardCoordinateGetter = (
  event,
  { currentCoordinates, context },
) =>
  nextColumnCoordinates(event.code, currentCoordinates, [
    ...(context.droppableRects?.values() ?? []),
  ]);
