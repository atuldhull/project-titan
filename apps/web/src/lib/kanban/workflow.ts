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
  return [...(index.get(fromStatus)?.keys() ?? [])];
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
