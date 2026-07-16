/**
 * The Kanban board's reconciliation core (TITAN-FRT-024).
 *
 * One pure reducer owns every writer of board state — the REST snapshot, the WS
 * frames, and the user's own optimistic drags — because "the board stays
 * consistent under concurrent agent + human moves, with no ghost cards" is a
 * property of how those three interleave, and a property you cannot test is a
 * property you do not have. No React, no fetch, no time: the chaos test drives
 * this directly.
 *
 * **No ghost cards is structural, not defended.** Cards live in ONE map keyed by
 * task id and a column is a grouping of that map, so a card is in exactly one
 * column by construction. There is no code path that could duplicate one, which
 * is why there is no de-duplication code to get wrong.
 *
 * **Server-wins.** A local drag is an override laid OVER server truth, never a
 * write into it, so dropping the override is the whole of rollback. Precisely:
 * an override is discarded by a frame that MOVES its task, by the move's own
 * response, or by its failure — but NOT by a REST snapshot (`hydrate` keeps it
 * deliberately; every frame triggers a refetch, so a snapshot lands mid-drag
 * constantly) and NOT by a frame that leaves the task's status alone.
 *
 * **Ordering.** `task.kanban` carries no `version` and no old status, so the only
 * ordering available is the per-project `seq`, which is monotonic across a
 * project's frames (with gaps — every category shares one counter). Frames are
 * ordered against each other by `seq`. An HTTP body is ordered against them by
 * whether the task's override survived: a mutation's response can land after a
 * frame that already superseded it, and `settleMove` drops it when it has.
 *
 * REST snapshots are last-writer-wins by arrival: a refetch that raced a frame
 * may briefly show the older of the two, and the next frame or refetch corrects
 * it. That is bounded because every task frame invalidates the board query, so
 * the last event in any burst is always a full snapshot.
 */

import type { components } from "@titan/schemas";
import {
  type ServerFrame,
  parseTaskKanban,
  type TaskKanbanPayload,
} from "@titan/ws-protocol";

export type Card = components["schemas"]["BoardCard"];
export type Board = components["schemas"]["Board"];

/** A drag the user made that the server has not yet confirmed or refused. */
export interface PendingMove {
  /** Where the card sat when the drag started — for the rollback announcement. */
  readonly fromStatus: string;
  readonly toStatus: string;
}

export interface BoardState {
  readonly projectId: string;
  /** Server truth. One entry per task — the no-ghost-cards guarantee. */
  readonly cards: ReadonlyMap<string, Card>;
  /**
   * Optimistic overrides, laid over `cards` at render time. Doubles as the
   * "has the server spoken about this task since we asked?" record: any frame
   * about a task clears its entry, which is what lets `settleMove` tell a fresh
   * response from one a frame already superseded.
   */
  readonly pending: ReadonlyMap<string, PendingMove>;
  /** Highest project `seq` applied. Gaps are normal (shared counter). */
  readonly lastSeq: number;
}

/** Something the reducer observed that the UI must react to. */
export type BoardEffect =
  | {
      /** The server moved this card somewhere other than where we dropped it. */
      readonly kind: "conflict";
      readonly taskId: string;
      readonly title: string;
      readonly attempted: string;
      readonly actual: string;
    }
  | {
      /**
       * A move we did not make, which NAMED an agent — animate it and pulse that
       * agent (§6.3). Deliberately not called "an agent moved it": the frame's
       * `actor_agent_id` is the agent the transition named (for `assign` that is
       * the assignee, which a human may have named), and the API cannot say
       * which. So this draws attention to the agent involved; it does not claim
       * that agent performed the move.
       */
      readonly kind: "agent-move";
      readonly taskId: string;
      readonly actorAgentId: string;
      readonly fromStatus: string;
      readonly toStatus: string;
    };

export interface BoardColumnView {
  readonly status: string;
  readonly cards: readonly Card[];
}

export function emptyBoard(projectId: string): BoardState {
  return {
    projectId,
    cards: new Map(),
    pending: new Map(),
    lastSeq: 0,
  };
}

function withEntry<V>(
  map: ReadonlyMap<string, V>,
  key: string,
  value: V,
): ReadonlyMap<string, V> {
  const next = new Map(map);
  next.set(key, value);
  return next;
}

function withoutEntry<V>(
  map: ReadonlyMap<string, V>,
  key: string,
): ReadonlyMap<string, V> {
  if (!map.has(key)) {
    return map;
  }
  const next = new Map(map);
  next.delete(key);
  return next;
}

/** The status a card renders in: the optimistic override, else server truth. */
export function effectiveStatus(
  state: BoardState,
  taskId: string,
): string | null {
  const card = state.cards.get(taskId);
  if (!card) {
    return null;
  }
  return state.pending.get(taskId)?.toStatus ?? card.status;
}

/**
 * Adopt a REST snapshot. Deliberately preserves `pending`: a refetch landing
 * mid-drag must not cancel the user's move (every frame triggers a refetch, so
 * that would happen constantly).
 */
export function hydrate(state: BoardState, board: Board): BoardState {
  const cards = new Map<string, Card>();
  for (const column of board.columns) {
    for (const card of column.tasks) {
      cards.set(card.id, card);
    }
  }
  return { ...state, cards };
}

/** The full task payload on `task.created` / `task.updated` (a `TaskOut`). */
function parseTaskFrame(payload: unknown): Card | null {
  if (typeof payload !== "object" || payload === null) {
    return null;
  }
  const p = payload as Record<string, unknown>;
  if (
    typeof p.id !== "string" ||
    typeof p.title !== "string" ||
    typeof p.status !== "string" ||
    typeof p.priority !== "number"
  ) {
    return null;
  }
  return {
    id: p.id,
    title: p.title,
    status: p.status,
    priority: p.priority,
    estimate_points:
      typeof p.estimate_points === "number" ? p.estimate_points : null,
    assignee_agent_id:
      typeof p.assignee_agent_id === "string" ? p.assignee_agent_id : null,
    sprint_id: typeof p.sprint_id === "string" ? p.sprint_id : null,
  };
}

function cardFromKanban(payload: TaskKanbanPayload, existing: Card): Card {
  // A kanban frame is a PATCH, not a card: it has no sprint_id and no
  // estimate_points, so those survive from the card we already hold rather than
  // being nulled out (which would silently un-size a task on every move).
  return {
    ...existing,
    title: payload.title,
    status: payload.status,
    priority: payload.priority,
    assignee_agent_id: payload.assignee_agent_id,
  };
}

/**
 * Apply one server frame. Returns the new state plus anything the UI must react
 * to. Frames for other projects and replays we have already seen are no-ops.
 */
export function applyFrame(
  state: BoardState,
  frame: ServerFrame,
): { state: BoardState; effects: readonly BoardEffect[] } {
  const none = { state, effects: [] as readonly BoardEffect[] };
  // `seq` is per PROJECT, so another project's frame is a different sequence
  // entirely — letting one touch lastSeq would blind us to our own frames.
  if (frame.project_id !== state.projectId) {
    return none;
  }
  if (frame.seq <= state.lastSeq) {
    // A server-side counter RESET (the Redis seq key expired or was lost) looks
    // exactly like a replay from here, and treating it as one makes the board
    // deaf to that project for the rest of the session. `WsClient` re-baselines
    // on the same signal and forwards the frame rather than swallowing it, so
    // this must too or the socket's recovery stops at this reducer's door.
    if (!(frame.seq === 1 && state.lastSeq > 1)) {
      return none; // duplicate, or the overlap of a reconnect replay
    }
  }
  // Only a TASK frame advances the cursor. It exists solely to drop replayed
  // task frames, and a frame this board ignores is one it cannot have applied —
  // so returning the state unchanged is both correct and what keeps an unrelated
  // frame (a run delta on a shared socket) from re-rendering the whole board.
  if (
    frame.type !== "task.created" &&
    frame.type !== "task.updated" &&
    frame.type !== "task.kanban"
  ) {
    return none;
  }
  const seen: BoardState = { ...state, lastSeq: frame.seq };

  let taskId: string;
  let nextCard: Card;
  let actorAgentId: string | null = null;

  if (frame.type === "task.created" || frame.type === "task.updated") {
    const card = parseTaskFrame(frame.payload);
    if (!card) {
      return { state: seen, effects: [] };
    }
    taskId = card.id;
    nextCard = card;
  } else {
    const payload = parseTaskKanban(frame.payload);
    if (!payload) {
      return { state: seen, effects: [] };
    }
    const existing = seen.cards.get(payload.task_id);
    if (!existing) {
      // A move for a task we have never seen. A kanban frame cannot build a
      // complete card (no sprint_id, no estimate_points), and inventing nulls
      // would render a task as unsized/unsprinted when it may be neither. The
      // board query is invalidated by this same frame, so the refetch brings it
      // in properly.
      return { state: seen, effects: [] };
    }
    taskId = payload.task_id;
    nextCard = cardFromKanban(payload, existing);
    actorAgentId = payload.actor_agent_id;
  }

  const previous = seen.cards.get(taskId);
  const effects: BoardEffect[] = [];
  const pendingMove = seen.pending.get(taskId);
  let pending = seen.pending;

  // ONLY a frame that MOVES the task says anything about a pending move. Plenty
  // of task frames carry no status change by construction — adding a dependency
  // bumps the version and emits `task.updated` with the status untouched; a
  // budget resume emits a `task.kanban` whose from_status equals its to_status.
  // Treating those as a verdict would drop the user's override mid-flight, toast
  // a phantom "your move was superseded", and then discard the move's own
  // response (`settleMove` reads the missing override as "a frame spoke").
  const movedColumn =
    previous !== undefined && previous.status !== nextCard.status;

  if (pendingMove && movedColumn) {
    // The server has moved this task: our override is settled either way.
    pending = withoutEntry(pending, taskId);
    if (nextCard.status !== pendingMove.toStatus) {
      effects.push({
        kind: "conflict",
        taskId,
        title: nextCard.title,
        attempted: pendingMove.toStatus,
        actual: nextCard.status,
      });
    }
  } else if (
    !pendingMove &&
    movedColumn &&
    previous !== undefined &&
    actorAgentId !== null
  ) {
    // An agent moved a card we were not touching: animate it (§6.3). Gated on a
    // real column change, so an intra-lane action never animates a card in place.
    effects.push({
      kind: "agent-move",
      taskId,
      actorAgentId,
      fromStatus: previous.status,
      toStatus: nextCard.status,
    });
  }

  return {
    state: {
      ...seen,
      cards: withEntry(seen.cards, taskId, nextCard),
      pending,
    },
    effects,
  };
}

/** Lay an optimistic override for a drag the user just made. */
export function beginMove(
  state: BoardState,
  taskId: string,
  toStatus: string,
): BoardState {
  const from = effectiveStatus(state, taskId);
  if (from === null || from === toStatus) {
    return state;
  }
  return {
    ...state,
    pending: withEntry(state.pending, taskId, { fromStatus: from, toStatus }),
  };
}

/**
 * The move's HTTP response came back OK.
 *
 * The body is server truth, but not necessarily the LATEST truth: a frame may
 * have superseded it while the request was in flight, and applying a stale body
 * would rewind the board. The pending entry itself is the test. Any frame about
 * a task clears that task's override, so if the override is still here, nothing
 * has been said about this task since we issued the move — the body is the
 * newest thing we know, and we take it. If it is gone, a frame already settled
 * the question and the body is dropped on the floor.
 */
export function settleMove(
  state: BoardState,
  taskId: string,
  serverStatus: string,
): BoardState {
  if (!state.pending.has(taskId)) {
    return state; // a frame spoke first; it is newer than this response
  }
  const pending = withoutEntry(state.pending, taskId);
  const card = state.cards.get(taskId);
  if (!card) {
    return { ...state, pending };
  }
  return {
    ...state,
    pending,
    cards: withEntry(state.cards, taskId, { ...card, status: serverStatus }),
  };
}

/**
 * The move failed. Dropping the override IS the rollback — the card renders from
 * server truth again, which never changed (TITAN-FRT-015).
 */
export function failMove(state: BoardState, taskId: string): BoardState {
  return { ...state, pending: withoutEntry(state.pending, taskId) };
}

/**
 * Group cards into columns, in the workflow's canonical order.
 *
 * Every card is placed exactly once. A status outside `states` still gets a
 * column (appended) rather than vanishing — the same defensive move the API's own
 * board builder makes, so an unknown state is visible instead of silently
 * dropping tasks off the board.
 */
export function boardColumns(
  state: BoardState,
  states: readonly string[],
): BoardColumnView[] {
  const known = new Map<string, Card[]>(states.map((s) => [s, []]));
  const unknown = new Map<string, Card[]>();

  for (const [id, card] of state.cards) {
    const status = state.pending.get(id)?.toStatus ?? card.status;
    let bucket = known.get(status) ?? unknown.get(status);
    if (!bucket) {
      bucket = [];
      unknown.set(status, bucket);
    }
    bucket.push(card);
  }

  const order = (cards: Card[]): Card[] =>
    // Stable sort: ties keep map order, which is the API's (priority, created_at).
    [...cards].sort((a, b) => a.priority - b.priority);

  return [
    ...states.map((status) => ({
      status,
      cards: order(known.get(status) ?? []),
    })),
    ...[...unknown].map(([status, cards]) => ({ status, cards: order(cards) })),
  ];
}

/** Total cards on the board (pending overrides do not create or destroy any). */
export function cardCount(state: BoardState): number {
  return state.cards.size;
}
