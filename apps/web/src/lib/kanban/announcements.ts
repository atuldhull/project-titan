/**
 * Screen-reader announcements for board drag-and-drop (TITAN-FRT-025/043).
 *
 * dnd-kit renders these into an `aria-live` region for us; what it cannot do is
 * know that our droppables are workflow columns or that a given drop is illegal.
 * Pure string builders, so the announced text is asserted directly in tests
 * rather than inferred from a rendered live region.
 *
 * The announcements say what a keyboard user cannot see: whether the column
 * under the card would ACCEPT it. Announcing "over Done" while dragging from
 * Backlog would be telling someone the move is available when dropping there
 * does nothing (no action gets backlog -> done).
 */

import { statusLabel } from "@/lib/kanban/workflow";

/**
 * What dnd-kit reads out when a card first receives focus (TITAN-FRT-043).
 *
 * dnd-kit ships a default for this and the default is WRONG on this board. It
 * says "press space or enter to start dragging", because those are the sensor's
 * default start codes — but §10 gives Enter a different job here (it opens the
 * task), so the board narrows `keyboardCodes` to Space alone. Shipping the
 * default therefore tells every screen-reader user to press a key that does the
 * opposite of what they were told, which is worse than saying nothing: it is the
 * instructions themselves that are the trap.
 *
 * It also names the menu. §8's "Move to…" fallback is useless if the only place
 * it is announced is a button several tab stops away.
 */
export const BOARD_DRAG_INSTRUCTIONS =
  "To pick up a task, press space. Use the arrow keys to choose a column, space again to drop it there, and escape to cancel. Press enter instead to open the task, or use its move menu to change column without dragging.";

export interface AnnouncementContext {
  /** Title of the card being dragged. */
  readonly title: string;
  /** The column it started in. */
  readonly fromStatus: string;
  /** The column it is currently over, if any. */
  readonly overStatus?: string | null;
  /** Whether dropping on `overStatus` is a move this user can make. */
  readonly allowed?: boolean;
}

export function onDragStartMessage(ctx: AnnouncementContext): string {
  return `Picked up ${ctx.title} from ${statusLabel(ctx.fromStatus)}. Use the arrow keys to choose a column, space to drop, escape to cancel.`;
}

export function onDragOverMessage(ctx: AnnouncementContext): string {
  if (!ctx.overStatus) {
    return `${ctx.title} is no longer over a column. Dropping here will cancel the move.`;
  }
  if (ctx.overStatus === ctx.fromStatus) {
    return `${ctx.title} is back over ${statusLabel(ctx.fromStatus)}, where it started.`;
  }
  if (!ctx.allowed) {
    return `${ctx.title} is over ${statusLabel(ctx.overStatus)}, which will not accept it from ${statusLabel(ctx.fromStatus)}.`;
  }
  return `${ctx.title} is over ${statusLabel(ctx.overStatus)}. Press space to move it there.`;
}

export function onDragEndMessage(ctx: AnnouncementContext): string {
  if (!ctx.overStatus || ctx.overStatus === ctx.fromStatus) {
    return `${ctx.title} was returned to ${statusLabel(ctx.fromStatus)}.`;
  }
  if (!ctx.allowed) {
    return `${statusLabel(ctx.overStatus)} does not accept ${ctx.title} from ${statusLabel(ctx.fromStatus)}. It was returned.`;
  }
  return `${ctx.title} was moved to ${statusLabel(ctx.overStatus)}.`;
}

export function onDragCancelMessage(ctx: AnnouncementContext): string {
  return `Move cancelled. ${ctx.title} stays in ${statusLabel(ctx.fromStatus)}.`;
}

/** The move failed on the server — say so, and say the card went back. */
export function moveFailedMessage(title: string, fromStatus: string): string {
  return `Moving ${title} failed. It was returned to ${statusLabel(fromStatus)}.`;
}
