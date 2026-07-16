/**
 * The optimistic move: rollback + retry toast (TITAN-FRT-015).
 *
 * §6.3's AC is "kill API mid-drag on Kanban → card returns to origin column,
 * toast shown". This drives the REAL move path — the real hook, the real
 * reducer, the real React Query mutation, the real `sonner` call — with only the
 * transport faked. What it does not drive is dnd-kit's pointer plumbing (jsdom
 * has no layout, so a real drag cannot be simulated); the drop is invoked as the
 * drag handler invokes it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@titan/api-client";

import {
  emptyBoard,
  effectiveStatus,
  hydrate,
  type BoardState,
} from "@/lib/kanban/board-store";
import { useBoardMoves } from "@/lib/kanban/use-moves";
import { moveIndex, type WorkflowDefinition } from "@/lib/kanban/workflow";

const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError, warning: vi.fn() } }));

const post = vi.hoisted(() => vi.fn());
vi.mock("@/lib/query/browser-client", () => ({
  browserApi: () => ({ POST: post, GET: vi.fn() }),
}));

const WORKFLOW: WorkflowDefinition = {
  states: ["backlog", "ready", "assigned"],
  transitions: [
    {
      action: "ready",
      from_status: "backlog",
      to_status: "ready",
      required_permission: "task:transition",
      requires_actor_agent: false,
    },
    {
      action: "assign",
      from_status: "ready",
      to_status: "assigned",
      required_permission: "task:assign",
      requires_actor_agent: true,
    },
  ],
};

function task(id: string, title: string) {
  return {
    id,
    title,
    status: "backlog",
    priority: 3,
    estimate_points: 3,
    assignee_agent_id: null,
    sprint_id: null,
  };
}

function seed(): BoardState {
  return hydrate(emptyBoard("p1"), {
    project_id: "p1",
    columns: [
      {
        status: "backlog",
        tasks: [task("t1", "Payment API"), task("t2", "Auth API")],
      },
    ],
  });
}

/** t1 already in `ready`, where `assign` (which needs an agent) is legal. */
function readySeed(): BoardState {
  return hydrate(emptyBoard("p1"), {
    project_id: "p1",
    columns: [
      {
        status: "ready",
        tasks: [{ ...task("t1", "Payment API"), status: "ready" }],
      },
    ],
  });
}

/** Renders the hook and exposes the board state it is driving. */
function Probe({
  stateRef,
  onCommit,
}: {
  stateRef: { current: BoardState };
  onCommit: (s: BoardState) => void;
}) {
  const moves = useBoardMoves({
    stateRef,
    commit: onCommit,
    index: moveIndex(WORKFLOW),
  });
  return (
    <div>
      <span data-testid="status">
        {effectiveStatus(stateRef.current, "t1")}
      </span>
      <span data-testid="announcement">{moves.failureAnnouncement}</span>
      <span data-testid="agent-choice">{moves.agentChoice?.action ?? ""}</span>
      <button type="button" onClick={() => moves.attemptMove("t1", "ready")}>
        drop on ready
      </button>
      <button type="button" onClick={() => moves.attemptMove("t1", "assigned")}>
        drop on assigned
      </button>
      <button type="button" onClick={() => moves.attemptMove("t1", "backlog")}>
        drop on own column
      </button>
      <button type="button" onClick={() => moves.attemptMove("t2", "ready")}>
        drop t2 on ready
      </button>
      <button type="button" onClick={() => moves.confirmAgent("agent-7")}>
        confirm agent
      </button>
      <button type="button" onClick={moves.cancelAgent}>
        cancel agent
      </button>
    </div>
  );
}

function mount() {
  const ref = { current: seed() };
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  // Mirrors useBoardState's contract: commit is the ref's ONLY writer.
  const commit = (s: BoardState) => {
    ref.current = s;
    rerender();
  };
  const utils = render(
    <QueryClientProvider client={client}>
      <Probe stateRef={ref} onCommit={commit} />
    </QueryClientProvider>,
  );
  function rerender() {
    utils.rerender(
      <QueryClientProvider client={client}>
        <Probe stateRef={ref} onCommit={commit} />
      </QueryClientProvider>,
    );
  }
  return { ref, ...utils };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useBoardMoves", () => {
  it("rolls the card back and offers a retry when the API fails mid-drag", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      error: {
        type: "about:blank",
        title: "Conflict",
        status: 409,
        detail: "Cannot mark ready: not all dependencies are done.",
      },
      response: new Response(null, { status: 409 }),
    });
    const { ref } = mount();

    await user.click(screen.getByText("drop on ready"));

    // The card is back where the server still has it...
    expect(effectiveStatus(ref.current, "t1")).toBe("backlog");
    expect(ref.current.pending.size).toBe(0);

    // ...a toast explains WHY, in the server's own words — a client could never
    // have predicted this precondition...
    expect(toastError).toHaveBeenCalledTimes(1);
    const [message, options] = toastError.mock.calls[0];
    expect(message).toContain("Payment API");
    expect(message).toContain("not all dependencies are done");
    // ...and it carries a retry action (TITAN-FRT-015).
    expect(options.action.label).toBe("Retry");

    // ...and a screen-reader user is told too: dnd-kit already announced the
    // drop as done before the server refused it.
    expect(screen.getByTestId("announcement")).toHaveTextContent(
      "Moving Payment API failed. It was returned to Backlog.",
    );
  });

  it("retrying from the toast re-sends the move AND lands the card", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      error: { type: "about:blank", title: "Conflict", status: 409 },
      response: new Response(null, { status: 409 }),
    });
    const { ref } = mount();
    await user.click(screen.getByText("drop on ready"));
    expect(post).toHaveBeenCalledTimes(1);
    expect(effectiveStatus(ref.current, "t1")).toBe("backlog");

    post.mockResolvedValue({
      data: { id: "t1", status: "ready" },
      response: new Response(null, { status: 200 }),
    });
    await act(async () => {
      toastError.mock.calls[0][1].action.onClick();
    });

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1][1].body).toEqual({ action: "ready" });
    // The retry must re-lay the optimistic override the failure rolled off —
    // otherwise settleMove reads the missing override as "a frame already
    // spoke" and drops the successful response, stranding the card.
    expect(effectiveStatus(ref.current, "t1")).toBe("ready");
    expect(ref.current.pending.size).toBe(0);
  });

  it("keeps the card in its new column when the move succeeds", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      data: { id: "t1", status: "ready" },
      response: new Response(null, { status: 200 }),
    });
    const { ref } = mount();

    await user.click(screen.getByText("drop on ready"));

    expect(effectiveStatus(ref.current, "t1")).toBe("ready");
    expect(ref.current.pending.size).toBe(0);
    expect(toastError).not.toHaveBeenCalled();
    expect(post.mock.calls[0][1].body).toEqual({ action: "ready" });
  });

  it("sends the resolved ACTION, never a status", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      data: { id: "t1", status: "ready" },
      response: new Response(null, { status: 200 }),
    });
    mount();
    await user.click(screen.getByText("drop on ready"));
    // There is no status write in this API: `TaskPatch` forbids the field.
    expect(post.mock.calls[0][0]).toBe("/api/v1/tasks/{task_id}:transition");
    expect(post.mock.calls[0][1].body).not.toHaveProperty("status");
  });

  it("pauses for an agent instead of sending a move that would 409", async () => {
    const user = userEvent.setup();
    const { ref } = mount();
    ref.current = readySeed();

    await user.click(screen.getByText("drop on assigned"));

    // `assign` names the assignee; the API 409s without one. Nothing is sent.
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByTestId("agent-choice")).toHaveTextContent("assign");
  });

  it("carries the chosen agent into the request as actor_agent_id", async () => {
    // The whole point of the picker: `assign` 409s without an agent. Nothing
    // covered this seam end-to-end, so the agent could have been dropped between
    // the dialog and the wire with a green suite.
    const user = userEvent.setup();
    post.mockResolvedValue({
      data: { id: "t1", status: "assigned" },
      response: new Response(null, { status: 200 }),
    });
    const { ref } = mount();
    ref.current = readySeed();

    await user.click(screen.getByText("drop on assigned"));
    expect(post).not.toHaveBeenCalled(); // paused for the agent
    await user.click(screen.getByText("confirm agent"));

    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][1].body).toEqual({
      action: "assign",
      actor_agent_id: "agent-7",
    });
    expect(effectiveStatus(ref.current, "t1")).toBe("assigned");
    expect(screen.getByTestId("agent-choice")).toHaveTextContent("");
  });

  it("cancelling the picker sends nothing and moves nothing", async () => {
    const user = userEvent.setup();
    const { ref } = mount();
    ref.current = readySeed();

    await user.click(screen.getByText("drop on assigned"));
    await user.click(screen.getByText("cancel agent"));

    expect(post).not.toHaveBeenCalled();
    // The card never moved optimistically — there is nothing to roll back.
    expect(effectiveStatus(ref.current, "t1")).toBe("ready");
    expect(ref.current.pending.size).toBe(0);
    expect(screen.getByTestId("agent-choice")).toHaveTextContent("");
  });

  it("does nothing at all when the drop is not a legal move", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByText("drop on own column"));
    expect(post).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("rolls back a move whose request was still in flight when a second drag started", async () => {
    // The board shares ONE useMutation across every card. React Query's
    // MutationObserver holds a single callback slot and detaches itself from the
    // previous mutation on the next `mutate`, so with `mutate(v, {onError})` the
    // FIRST move's rollback silently never runs: its card sits in a column the
    // server refused, for the life of the page, with no toast. `mutateAsync`'s
    // per-call promise is what makes concurrent drags safe — this is the test
    // that would have caught it.
    const user = userEvent.setup();
    let failFirst: (() => void) | undefined;
    post
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            failFirst = () =>
              resolve({
                error: {
                  type: "about:blank",
                  title: "Conflict",
                  status: 409,
                  detail: "Cannot mark ready: not all dependencies are done.",
                },
                response: new Response(null, { status: 409 }),
              });
          }),
      )
      .mockResolvedValue({
        data: { id: "t2", status: "ready" },
        response: new Response(null, { status: 200 }),
      });

    const { ref } = mount();
    await user.click(screen.getByText("drop on ready")); // t1: in flight
    await user.click(screen.getByText("drop t2 on ready")); // t2: detaches t1

    await act(async () => {
      failFirst?.();
      await Promise.resolve();
    });

    // t1's failure must still roll back and still toast.
    expect(effectiveStatus(ref.current, "t1")).toBe("backlog");
    expect(ref.current.pending.has("t1")).toBe(false);
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(String(toastError.mock.calls[0][0])).toContain("Payment API");
    // ...and t2's success is unaffected.
    expect(effectiveStatus(ref.current, "t2")).toBe("ready");
  });

  it("surfaces a generic message when the failure carries no detail", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      error: undefined,
      response: new Response(null, { status: 500 }),
    });
    mount();
    await user.click(screen.getByText("drop on ready"));
    expect(toastError).toHaveBeenCalledTimes(1);
    // ApiError's fallback message, not an empty string.
    expect(String(toastError.mock.calls[0][0])).toContain("500");
  });
});

describe("ApiError message", () => {
  it("is the server's problem detail — what the toast shows", () => {
    const error = new ApiError(409, {
      type: "about:blank",
      title: "Conflict",
      status: 409,
      detail: "The reviewer must be different from the assignee.",
    });
    expect(error.message).toBe(
      "The reviewer must be different from the assignee.",
    );
  });
});
