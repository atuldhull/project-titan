/**
 * The board's reconciliation core (TITAN-FRT-024).
 *
 * Includes the phase's chaos requirement: 20 concurrent agent + human moves
 * converge to the API's state with no ghost or duplicated cards. This drives the
 * reducer directly rather than a browser — see the note on `chaos` below for
 * exactly what that does and does not prove.
 */

import { describe, expect, it } from "vitest";

import { PROTOCOL_VERSION, type ServerFrame } from "@titan/ws-protocol";

import {
  applyFrame,
  beginMove,
  boardColumns,
  cardCount,
  effectiveStatus,
  emptyBoard,
  failMove,
  hydrate,
  settleMove,
  type Board,
  type BoardState,
  type Card,
} from "@/lib/kanban/board-store";

const PID = "p1";
const STATES = ["backlog", "ready", "assigned", "in_progress", "done"];

function card(id: string, status: string, overrides: Partial<Card> = {}): Card {
  return {
    id,
    title: `Task ${id}`,
    status,
    priority: 3,
    estimate_points: null,
    assignee_agent_id: null,
    sprint_id: null,
    ...overrides,
  };
}

function board(cards: readonly Card[]): Board {
  const byStatus = new Map<string, Card[]>();
  for (const c of cards) {
    const bucket = byStatus.get(c.status) ?? [];
    bucket.push(c);
    byStatus.set(c.status, bucket);
  }
  return {
    project_id: PID,
    columns: [...byStatus].map(([status, tasks]) => ({ status, tasks })),
  };
}

function kanbanFrame(
  seq: number,
  taskId: string,
  status: string,
  opts: { actorAgentId?: string | null; projectId?: string } = {},
): ServerFrame {
  return {
    v: PROTOCOL_VERSION,
    type: "task.kanban",
    seq,
    ts: "2026-07-16T00:00:00Z",
    project_id: opts.projectId ?? PID,
    payload: {
      task_id: taskId,
      project_id: opts.projectId ?? PID,
      title: `Task ${taskId}`,
      status,
      lane: status.toUpperCase(),
      from_lane: "READY",
      action: "move",
      priority: 3,
      assignee_agent_id: null,
      actor_agent_id: opts.actorAgentId ?? null,
    },
  };
}

function seeded(cards: readonly Card[]): BoardState {
  return hydrate(emptyBoard(PID), board(cards));
}

describe("hydrate", () => {
  it("indexes every card by id", () => {
    const state = seeded([card("a", "backlog"), card("b", "done")]);
    expect(cardCount(state)).toBe(2);
    expect(effectiveStatus(state, "a")).toBe("backlog");
  });

  it("keeps an in-flight move — a refetch must not cancel a drag", () => {
    // Every frame invalidates the board, so a refetch lands mid-drag routinely.
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    state = hydrate(state, board([card("a", "backlog")]));
    expect(effectiveStatus(state, "a")).toBe("ready");
  });
});

describe("applyFrame", () => {
  it("moves a card on task.kanban", () => {
    let state = seeded([card("a", "backlog")]);
    ({ state } = applyFrame(state, kanbanFrame(1, "a", "ready")));
    expect(effectiveStatus(state, "a")).toBe("ready");
  });

  it("ignores a replayed frame (seq <= lastSeq)", () => {
    let state = seeded([card("a", "backlog")]);
    ({ state } = applyFrame(state, kanbanFrame(5, "a", "ready")));
    ({ state } = applyFrame(state, kanbanFrame(5, "a", "done")));
    ({ state } = applyFrame(state, kanbanFrame(3, "a", "done")));
    expect(effectiveStatus(state, "a")).toBe("ready");
  });

  it("ignores another project's frame entirely", () => {
    // seq is per project: letting p2's frame bump lastSeq would blind us to p1's.
    let state = seeded([card("a", "backlog")]);
    ({ state } = applyFrame(
      state,
      kanbanFrame(99, "a", "done", { projectId: "p2" }),
    ));
    expect(effectiveStatus(state, "a")).toBe("backlog");
    expect(state.lastSeq).toBe(0);
  });

  it("preserves points and sprint through a kanban patch", () => {
    // A kanban frame carries neither field. Spreading it over the card would
    // silently un-size the task on every move.
    let state = seeded([
      card("a", "backlog", { estimate_points: 5, sprint_id: "s1" }),
    ]);
    ({ state } = applyFrame(state, kanbanFrame(1, "a", "ready")));
    expect(state.cards.get("a")?.estimate_points).toBe(5);
    expect(state.cards.get("a")?.sprint_id).toBe("s1");
  });

  it("returns the SAME state object for a frame it does not handle", () => {
    // Identity matters: the board re-renders on any new state object, and the
    // socket is shared — a run.output.delta burst would otherwise re-render the
    // whole board once per token. It also must not advance a cursor it never uses.
    const state = seeded([card("a", "backlog")]);
    const result = applyFrame(state, {
      v: PROTOCOL_VERSION,
      type: "run.output.delta",
      seq: 9,
      ts: "2026-07-16T00:00:00Z",
      project_id: PID,
      payload: { delta: "x" },
    });
    expect(result.state).toBe(state);
    expect(result.state.lastSeq).toBe(0);
  });

  it("skips a kanban frame for a task it has never seen", () => {
    // It cannot build a complete card (no points, no sprint) — inventing nulls
    // would render the task as unsized. The refetch brings it in properly.
    let state = seeded([]);
    ({ state } = applyFrame(state, kanbanFrame(1, "ghost", "ready")));
    expect(cardCount(state)).toBe(0);
    expect(state.lastSeq).toBe(1);
  });

  it("adds a card from task.created (a full task payload)", () => {
    let state = seeded([]);
    const frame: ServerFrame = {
      v: PROTOCOL_VERSION,
      type: "task.created",
      seq: 1,
      ts: "2026-07-16T00:00:00Z",
      project_id: PID,
      payload: {
        id: "new",
        project_id: PID,
        title: "New task",
        status: "backlog",
        priority: 2,
        estimate_points: 3,
        assignee_agent_id: null,
        sprint_id: null,
      },
    };
    ({ state } = applyFrame(state, frame));
    expect(state.cards.get("new")?.estimate_points).toBe(3);
  });

  it("reports an agent move so the UI can animate and attribute it", () => {
    let state = seeded([card("a", "backlog")]);
    const result = applyFrame(
      state,
      kanbanFrame(1, "a", "ready", { actorAgentId: "agent-1" }),
    );
    state = result.state;
    const effects = result.effects;
    expect(effects).toEqual([
      {
        kind: "agent-move",
        taskId: "a",
        actorAgentId: "agent-1",
        fromStatus: "backlog",
        toStatus: "ready",
      },
    ]);
  });

  it("does not report an agent move when the status did not change", () => {
    // assigned -> in_progress is one LANE, and other actions do not move a
    // column at all; animating a card that stayed put is just noise.
    let state = seeded([card("a", "ready")]);
    const result = applyFrame(
      state,
      kanbanFrame(1, "a", "ready", { actorAgentId: "agent-1" }),
    );
    state = result.state;
    const effects = result.effects;
    expect(effects).toEqual([]);
  });

  it("does not report an agent move for a human's move (no actor agent)", () => {
    let state = seeded([card("a", "backlog")]);
    const result = applyFrame(state, kanbanFrame(1, "a", "ready"));
    state = result.state;
    const effects = result.effects;
    expect(effects).toEqual([]);
  });
});

describe("server-wins", () => {
  it("confirms our move silently when the server agrees", () => {
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    const result = applyFrame(state, kanbanFrame(1, "a", "ready"));
    state = result.state;
    const effects = result.effects;
    expect(effects).toEqual([]);
    expect(state.pending.size).toBe(0);
    expect(effectiveStatus(state, "a")).toBe("ready");
  });

  it("ignores a frame that moves nothing — it is not a verdict on our move", () => {
    // Adding a dependency bumps the version and emits task.updated with the
    // status UNTOUCHED; a budget resume emits task.kanban with from == to.
    // Treating either as a verdict would yank the card back mid-flight, toast a
    // phantom "superseded", and then make settleMove discard the real response.
    let state = seeded([card("a", "in_progress")]);
    state = beginMove(state, "a", "in_review");
    const result = applyFrame(state, {
      v: PROTOCOL_VERSION,
      type: "task.updated",
      seq: 4,
      ts: "2026-07-16T00:00:00Z",
      project_id: PID,
      payload: {
        id: "a",
        title: "Task a",
        status: "in_progress",
        priority: 3,
        estimate_points: null,
        assignee_agent_id: null,
        sprint_id: null,
      },
    });
    expect(result.effects).toEqual([]);
    expect(result.state.pending.has("a")).toBe(true);
    expect(effectiveStatus(result.state, "a")).toBe("in_review");
  });

  it("recovers when the server's seq counter restarts at 1", () => {
    // The Redis seq key can expire or be lost. A restart looks exactly like a
    // replay; treating it as one makes the board deaf for the session. WsClient
    // re-baselines and FORWARDS the frame, so the reducer must accept it.
    let state = seeded([card("a", "backlog")]);
    ({ state } = applyFrame(state, kanbanFrame(40, "a", "ready")));
    ({ state } = applyFrame(state, kanbanFrame(1, "a", "done")));
    expect(effectiveStatus(state, "a")).toBe("done");
    expect(state.lastSeq).toBe(1);
    // ...and it is still a real cursor afterwards.
    ({ state } = applyFrame(state, kanbanFrame(1, "a", "backlog")));
    expect(effectiveStatus(state, "a")).toBe("done");
  });

  it("discards our move and reports a conflict when the server disagrees", () => {
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    const result = applyFrame(state, kanbanFrame(1, "a", "done"));
    state = result.state;
    const effects = result.effects;
    expect(effectiveStatus(state, "a")).toBe("done");
    expect(state.pending.size).toBe(0);
    expect(effects).toEqual([
      {
        kind: "conflict",
        taskId: "a",
        title: "Task a",
        attempted: "ready",
        actual: "done",
      },
    ]);
  });
});

describe("settleMove", () => {
  it("applies the HTTP body when no frame has spoken since", () => {
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    state = settleMove(state, "a", "ready");
    expect(effectiveStatus(state, "a")).toBe("ready");
    expect(state.pending.size).toBe(0);
  });

  it("drops the HTTP body when a frame superseded it in flight", () => {
    // The response can land after a frame that already moved the task on. The
    // body is server truth but STALE truth; applying it would rewind the board.
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    ({ state } = applyFrame(state, kanbanFrame(7, "a", "done")));
    state = settleMove(state, "a", "ready");
    expect(effectiveStatus(state, "a")).toBe("done");
  });
});

describe("failMove", () => {
  it("rolls the card back to where the server still has it", () => {
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    expect(effectiveStatus(state, "a")).toBe("ready");
    state = failMove(state, "a");
    expect(effectiveStatus(state, "a")).toBe("backlog");
    expect(cardCount(state)).toBe(1);
  });
});

describe("boardColumns", () => {
  it("places every card exactly once, in workflow order", () => {
    const state = seeded([
      card("a", "backlog"),
      card("b", "done"),
      card("c", "backlog"),
    ]);
    const columns = boardColumns(state, STATES);
    expect(columns.map((c) => c.status)).toEqual(STATES);
    expect(columns[0].cards.map((c) => c.id)).toEqual(["a", "c"]);
    expect(columns[4].cards.map((c) => c.id)).toEqual(["b"]);
  });

  it("shows an optimistic card in its target column only", () => {
    let state = seeded([card("a", "backlog")]);
    state = beginMove(state, "a", "ready");
    const columns = boardColumns(state, STATES);
    expect(columns[0].cards).toEqual([]);
    expect(columns[1].cards.map((c) => c.id)).toEqual(["a"]);
  });

  it("sorts by priority, keeping API order within a priority", () => {
    const state = seeded([
      card("a", "backlog", { priority: 5 }),
      card("b", "backlog", { priority: 1 }),
      card("c", "backlog", { priority: 5 }),
    ]);
    expect(boardColumns(state, STATES)[0].cards.map((c) => c.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
  });

  it("surfaces an unknown status in its own column rather than dropping it", () => {
    const state = seeded([card("a", "some_new_state")]);
    const columns = boardColumns(state, STATES);
    expect(columns.at(-1)).toEqual({
      status: "some_new_state",
      cards: [state.cards.get("a")],
    });
  });
});

// --- the chaos requirement --------------------------------------------------

/**
 * A deterministic 32-bit PRNG, so a failure is reproducible from its seed.
 */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0x100000000;
  };
}

describe("chaos: 20 concurrent agent + human moves (TITAN-FRT-024)", () => {
  /**
   * What this proves and what it does not. Read this before trusting it.
   *
   * PROVES: under 20 moves interleaved arbitrarily — agent frames landing while
   * the human's own drags are still in flight, responses arriving out of order,
   * failures, duplicate frames — the reducer never loses, duplicates or strands a
   * card. That is the per-step invariant below, which runs on every one of the 20
   * steps in a genuinely mixed state, and it is the load-bearing assertion here.
   *
   * DOES NOT PROVE — two limits, both real:
   *
   * 1. The FINAL equality against `server` is weaker than it looks. The closing
   *    `hydrate` rebuilds `cards` wholesale from the same oracle it is compared
   *    to, so that assertion mostly proves hydrate adopts a snapshot and that no
   *    override survived it. It is not independent evidence of mid-flight
   *    convergence; the per-step invariant is.
   * 2. A browser does not run this. It drives the reducer, not Playwright against
   *    a live API — there is no E2E harness in this repo. The reducer is where
   *    every ordering decision lives, so this is the whole of the LOGIC, but it
   *    is not the browser test §6.3's AC asks for and must not be read as one.
   */
  it.each([1, 2, 3, 7, 42, 1337])("converges (seed %i)", (seed) => {
    const random = rng(seed);
    const ids = ["t1", "t2", "t3", "t4", "t5"];
    let state = seeded(ids.map((id) => card(id, "backlog")));

    // The server's truth, evolved independently of the client.
    const server = new Map(ids.map((id) => [id, "backlog"]));
    let seq = 0;
    const inFlight: { taskId: string; target: string; ok: boolean }[] = [];

    const pick = <T>(xs: readonly T[]): T =>
      xs[Math.floor(random() * xs.length)];

    for (let i = 0; i < 20; i += 1) {
      const roll = random();
      const taskId = pick(ids);

      if (roll < 0.4) {
        // An AGENT moves it: the server changes, then the frame arrives.
        const target = pick(STATES);
        server.set(taskId, target);
        seq += 1;
        ({ state } = applyFrame(
          state,
          kanbanFrame(seq, taskId, target, { actorAgentId: "agent-1" }),
        ));
      } else if (roll < 0.7) {
        // A HUMAN drags it: optimistic now, resolved later (below).
        const target = pick(STATES);
        const ok = random() < 0.7;
        state = beginMove(state, taskId, target);
        if (ok) {
          server.set(taskId, target);
        }
        inFlight.push({ taskId, target, ok });
      } else if (roll < 0.85 && inFlight.length > 0) {
        // A pending request resolves — possibly long after later frames landed.
        const move = inFlight.splice(
          Math.floor(random() * inFlight.length),
          1,
        )[0];
        state = move.ok
          ? settleMove(state, move.taskId, move.target)
          : failMove(state, move.taskId);
      } else if (seq > 0) {
        // A genuinely replayed frame: a seq we have already applied, carrying a
        // status we must NOT adopt. (Guarded on seq > 0 — before the first real
        // frame there is nothing to replay, and emitting seq 1 here would be a
        // novel frame wearing a replay's label.)
        const before = effectiveStatus(state, taskId);
        ({ state } = applyFrame(state, kanbanFrame(seq, taskId, "done")));
        expect(effectiveStatus(state, taskId)).toBe(before);
      }

      // INVARIANTS, checked at every one of the 20 steps, in a genuinely mixed
      // state (overrides in flight, frames landed, responses outstanding).
      const columns = boardColumns(state, STATES);
      const placed = columns.flatMap((c) => c.cards.map((card) => card.id));
      // Each card is in exactly one column — no ghosts, no duplicates, no losses.
      expect(placed.length).toBe(ids.length);
      expect(new Set(placed).size).toBe(ids.length);
      // Every column a card is placed in is a real workflow state, and every
      // override still standing points at the column the card actually renders
      // in. This is what would break if a move were stranded or mis-settled.
      for (const [taskId_, move] of state.pending) {
        expect(effectiveStatus(state, taskId_)).toBe(move.toStatus);
      }
      for (const column of columns) {
        for (const c of column.cards) {
          expect(effectiveStatus(state, c.id)).toBe(column.status);
        }
      }
    }

    // Everything still in flight resolves.
    for (const move of inFlight) {
      state = move.ok
        ? settleMove(state, move.taskId, move.target)
        : failMove(state, move.taskId);
    }
    // ...and the burst's final refetch lands: the board adopts the API's state.
    state = hydrate(
      state,
      board(ids.map((id) => card(id, server.get(id) ?? "backlog"))),
    );

    expect(state.pending.size).toBe(0);
    for (const id of ids) {
      expect(effectiveStatus(state, id)).toBe(server.get(id));
    }
    const columns = boardColumns(state, STATES);
    const placed = columns.flatMap((c) => c.cards.map((card) => card.id));
    expect(placed.sort()).toEqual([...ids].sort());
  });

  it("never duplicates a card even if the server reports it moving repeatedly", () => {
    let state = seeded([card("a", "backlog")]);
    for (let i = 1; i <= 50; i += 1) {
      ({ state } = applyFrame(state, kanbanFrame(i, "a", STATES[i % 5])));
      expect(cardCount(state)).toBe(1);
    }
    const placed = boardColumns(state, STATES).flatMap((c) => c.cards);
    expect(placed).toHaveLength(1);
  });
});
