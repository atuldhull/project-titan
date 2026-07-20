/**
 * The task workflow, as the board sees it (Vol 4 §6.3).
 *
 * The single most important fact about this board: **a drag is not a status
 * write.** There is no `PATCH /tasks/{id} {status}` — `TaskPatch` forbids the
 * field outright. State moves only through `POST /tasks/{id}:transition` with an
 * ACTION name, and the (from_status, action) -> to_status table is the backend's
 * (TITAN-BCK-012). So dropping a card on a column means resolving "which action
 * gets from A to B", and plenty of column pairs have no answer: backlog -> done
 * is not a move anyone can make.
 *
 * That table now ships from the API (`GET /api/v1/workflow`) rather than being
 * copied here, which is the only way this file cannot drift from the machine it
 * describes.
 *
 * Pure: no React, no fetch. Everything here is a projection of the definition.
 */

import type { components } from "@titan/schemas";

export type WorkflowDefinition = components["schemas"]["WorkflowDefinition"];
export type WorkflowTransition = components["schemas"]["WorkflowTransition"];

/** from_status -> to_status -> the single action that performs that move. */
export type MoveIndex = ReadonlyMap<
  string,
  ReadonlyMap<string, WorkflowTransition>
>;

/**
 * Index the definition for drop resolution.
 *
 * A (from, to) pair reachable by more than one action is AMBIGUOUS — a drag
 * carries no way to choose between them, so it is indexed to nothing and the
 * board declines to offer it rather than guessing. Today's table has no such
 * pair (pinned by a test); this is what keeps a future one from silently firing
 * the wrong action.
 */
export function moveIndex(definition: WorkflowDefinition): MoveIndex {
  const ambiguous = new Set<string>();
  const index = new Map<string, Map<string, WorkflowTransition>>();
  for (const transition of definition.transitions) {
    let row = index.get(transition.from_status);
    if (!row) {
      row = new Map();
      index.set(transition.from_status, row);
    }
    const key = `${transition.from_status}->${transition.to_status}`;
    if (row.has(transition.to_status)) {
      ambiguous.add(key);
      continue;
    }
    row.set(transition.to_status, transition);
  }
  for (const key of ambiguous) {
    const [from, to] = key.split("->");
    index.get(from)?.delete(to);
  }
  return index;
}

/**
 * The action that moves a card from `fromStatus` to `toStatus`, or null when no
 * single action does. Null means the board must refuse the drop: firing anything
 * would be a guaranteed 409.
 */
export function resolveMove(
  index: MoveIndex,
  fromStatus: string,
  toStatus: string,
): WorkflowTransition | null {
  if (fromStatus === toStatus) {
    return null;
  }
  return index.get(fromStatus)?.get(toStatus) ?? null;
}

/** Every status a card in `fromStatus` can be dragged to. */
export function legalTargets(index: MoveIndex, fromStatus: string): string[] {
  return [...(index.get(fromStatus)?.keys() ?? [])].filter(
    (to) => to !== fromStatus,
  );
}

/**
 * from-status → every status THIS user may move a card there to, in column
 * order.
 *
 * Keyed by from-status rather than computed for one card, because §8's "Move
 * to…" menu has to answer for a card with no drag in progress; the board then
 * narrows it to the dragged card's row for drop-target highlighting, so the menu
 * and the drag cannot disagree about what is legal.
 *
 * Three rules, and each of them is a bug that has a shape:
 *
 * - **`to === from` is dropped**, exactly as `resolveMove` drops it. The index is
 *   the raw (from, to) table, so a self-edge — an action whose `from_status` and
 *   `to_status` are equal, which the backend's machine is free to grow — would
 *   otherwise put a card's OWN column in its move menu (a dead item that
 *   silently no-ops, because `attemptMove` re-resolves it to null) and light the
 *   source column up as a legal drop target that then refuses the drop.
 * - **Built from the index, not from `states`.** A card can sit in a status the
 *   states list does not mention (`boardColumns` renders those as extra
 *   columns), and such a card must still be movable.
 * - **`order` only ORDERS.** The menu reads down the board the way the columns
 *   do; anything the machine allows that is not a listed column is appended
 *   rather than dropped.
 */
export function permittedTargets(
  index: MoveIndex | null,
  can: (permission: string) => boolean,
  order: readonly string[],
): ReadonlyMap<string, readonly string[]> {
  const out = new Map<string, readonly string[]>();
  if (!index) {
    return out;
  }
  for (const [from, row] of index) {
    const allowed = new Set<string>();
    for (const [to, transition] of row) {
      if (to !== from && can(transition.required_permission)) {
        allowed.add(to);
      }
    }
    out.set(from, [
      ...order.filter((status) => allowed.has(status)),
      ...[...allowed].filter((status) => !order.includes(status)),
    ]);
  }
  return out;
}

/**
 * Whether a drop is offerable to THIS user: a legal edge they hold the
 * permission for. `can` is `useCan`'s matcher, so wildcards resolve exactly as
 * the backend's `permission_granted` does.
 *
 * Legality here is necessary but NOT sufficient — the server additionally
 * enforces preconditions no client can evaluate (`ready` needs its dependencies
 * done; `complete` needs a reflection on record; a done-entry needs no unflushed
 * working memory and no Security veto). Those arrive as a 409 with a readable
 * detail, which is why the move path must surface that detail rather than
 * pretending it predicted it.
 */
export function canMove(
  index: MoveIndex,
  can: (permission: string) => boolean,
  fromStatus: string,
  toStatus: string,
): boolean {
  const transition = resolveMove(index, fromStatus, toStatus);
  return transition !== null && can(transition.required_permission);
}

/** Human-readable label for an action, for announcements and toasts. */
export function actionLabel(action: string): string {
  return action.replace(/_/g, " ");
}

/** Title-case a status for a column heading: `in_progress` -> `In progress`. */
export function statusLabel(status: string): string {
  const spaced = status.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
