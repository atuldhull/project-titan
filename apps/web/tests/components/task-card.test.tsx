/**
 * The card's keyboard contract (Vol 4 §10, TITAN-FRT-025/042).
 *
 * §10 gives Enter and Space DIFFERENT jobs on this screen — Enter opens, Space
 * lifts — and dnd-kit's KeyboardSensor claims both by default. These render the
 * real card inside a real DndContext with the board's own sensor configuration,
 * so a regression in that configuration fails here rather than in a browser.
 */

import {
  DndContext,
  KeyboardSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { TaskCard, shortTaskId } from "@/components/kanban/task-card";
import type { Card } from "@/lib/kanban/board-store";

const CARD: Card = {
  id: "0192f4a1-1111-7000-8000-000000000001",
  title: "Payment API",
  status: "backlog",
  priority: 3,
  estimate_points: 5,
  assignee_agent_id: null,
  sprint_id: null,
};

/** The board's real sensor setup: Space starts a drag, Enter does not. */
function Harness({
  onOpen,
  onNavigate,
  onDragStart,
  onMove,
  moveTargets,
  card = CARD,
  draggable = true,
  unmetDeps = 0,
}: {
  onOpen?: (id: string) => void;
  onNavigate?: (id: string, key: string) => void;
  onDragStart?: () => void;
  onMove?: (id: string, toStatus: string) => void;
  moveTargets?: readonly string[];
  card?: Card;
  draggable?: boolean;
  unmetDeps?: number;
}) {
  const sensors = useSensors(
    useSensor(KeyboardSensor, {
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space"] },
    }),
  );
  return (
    <TooltipProvider>
      <DndContext sensors={sensors} onDragStart={onDragStart}>
        <ul>
          <TaskCard
            card={card}
            agent={undefined}
            unmetDeps={unmetDeps}
            draggable={draggable}
            pulsing={false}
            animating={false}
            moveTargets={moveTargets}
            onMove={onMove}
            onOpen={onOpen ?? (() => undefined)}
            onNavigate={onNavigate}
          />
        </ul>
      </DndContext>
    </TooltipProvider>
  );
}

describe("TaskCard keyboard contract", () => {
  it("LIFTS the card on Space (§10) — the card's own onKeyDown must delegate", async () => {
    // The regression this exists for: dnd-kit's KeyboardSensor activator IS an
    // `onKeyDown` inside `listeners`, so the card's explicit `onKeyDown` prop
    // overwrites it. A handler that fails to call through leaves every card
    // unliftable — silently, with a green suite — and TITAN-FRT-025 is a MUST.
    const user = userEvent.setup();
    const onDragStart = vi.fn();
    render(<Harness onDragStart={onDragStart} />);

    await user.tab();
    await user.keyboard("{ }");

    expect(onDragStart).toHaveBeenCalledTimes(1);
  });

  it("does not lift a card the user may not move", async () => {
    const user = userEvent.setup();
    const onDragStart = vi.fn();
    render(<Harness draggable={false} onDragStart={onDragStart} />);
    await user.tab();
    await user.keyboard("{ }");
    expect(onDragStart).not.toHaveBeenCalled();
  });

  it("opens the sheet on Enter, and does NOT lift the card", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const onDragStart = vi.fn();
    render(<Harness onOpen={onOpen} onDragStart={onDragStart} />);

    await user.tab();
    await user.keyboard("{Enter}");

    expect(onOpen).toHaveBeenCalledWith(CARD.id);
    // dnd-kit's default start codes are ['Space','Enter']. If the board ever
    // stopped narrowing them to Space, Enter would lift instead of open — so
    // assert the LIFT did not happen, not merely that some text is absent.
    expect(onDragStart).not.toHaveBeenCalled();
  });

  it("moves focus on arrow keys while nothing is lifted", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<Harness onNavigate={onNavigate} />);

    await user.tab();
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{ArrowDown}");

    expect(onNavigate.mock.calls).toEqual([
      [CARD.id, "ArrowRight"],
      [CARD.id, "ArrowDown"],
    ]);
  });

  it("gives the card exactly one tab stop and one accessible name", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    // One control per card: a separate grip button would announce every card
    // twice and double the tab distance down a 40-card column.
    const controls = screen.getAllByRole("button");
    expect(controls).toHaveLength(1);
    await user.tab();
    expect(controls[0]).toHaveFocus();
  });

  it("opens on a pointer click (the drag sensor needs movement first)", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<Harness onOpen={onOpen} />);
    await user.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalledWith(CARD.id);
  });

  it("tells a screen-reader user both keys are available", () => {
    render(<Harness />);
    expect(
      screen.getByRole("button", {
        name: /press enter to open, space to move/i,
      }),
    ).toBeInTheDocument();
  });

  it("offers no move to someone who cannot make one", () => {
    // A viewer holds *:read only — every drag would 403.
    render(<Harness draggable={false} />);
    const handle = screen.getByRole("button", { name: /Payment API/ });
    expect(handle).toHaveAccessibleName(/press enter to open/i);
    expect(handle).not.toHaveAccessibleName(/space to move/i);
  });

  it("still opens on Enter when the card is not draggable", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<Harness draggable={false} onOpen={onOpen} />);
    await user.tab();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledWith(CARD.id);
  });
});

describe("TaskCard 'Move to…' menu (§8's DnD fallback)", () => {
  const TARGETS = ["ready", "blocked"] as const;

  it("moves the card without any drag at all", async () => {
    // §8 requires this verbatim: drag-and-drop "gains long-press initiation on
    // touch plus an explicit 'Move to…' menu fallback". It is also the only way
    // to move a card that does not require holding a gesture, which is what
    // makes the board operable with a tremor or through switch access.
    const user = userEvent.setup();
    const onMove = vi.fn();
    render(<Harness moveTargets={[...TARGETS]} onMove={onMove} />);

    await user.click(
      screen.getByRole("button", {
        name: /move payment api to another column/i,
      }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Blocked" }));

    expect(onMove).toHaveBeenCalledWith(CARD.id, "blocked");
  });

  it("does NOT shadow dnd-kit's activator — the card still lifts on Space", async () => {
    // The Phase 59 trap, re-armed by this menu. dnd-kit's KeyboardSensor
    // activator IS an `onKeyDown` inside the draggable's `listeners`, so any
    // control that ends up INSIDE that button and handles keys of its own makes
    // every card silently unliftable. The menu is deliberately a sibling; this
    // fails the moment someone moves it in.
    const user = userEvent.setup();
    const onDragStart = vi.fn();
    render(
      <Harness
        moveTargets={[...TARGETS]}
        onMove={vi.fn()}
        onDragStart={onDragStart}
      />,
    );

    await user.tab(); // the card's own control comes first
    await user.keyboard("{ }");

    expect(onDragStart).toHaveBeenCalledTimes(1);
  });

  it("offers no menu when the workflow allows this card nowhere", () => {
    // A terminal status, or a viewer who holds no transition permission. An
    // empty menu behind a live-looking button reads to a screen reader as an
    // available action that then is not one.
    render(<Harness moveTargets={[]} onMove={vi.fn()} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("offers no menu when the board wires no move handler", () => {
    render(<Harness moveTargets={[...TARGETS]} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("lists exactly the targets it was handed, in the order it was handed them", () => {
    // The board hands down only what the workflow index allows from this card's
    // status for this user's permissions. The card must not re-sort or filter:
    // the order is the board's column order, which is how a user reads it.
    render(<Harness moveTargets={["blocked", "ready"]} onMove={vi.fn()} />);
    return userEvent
      .setup()
      .click(screen.getByRole("button", { name: /move payment api/i }))
      .then(async () => {
        const items = await screen.findAllByRole("menuitem");
        expect(items.map((item) => item.textContent)).toEqual([
          "Blocked",
          "Ready",
        ]);
      });
  });
});

describe("TaskCard content", () => {
  it("renders points when the task is sized", () => {
    render(<Harness />);
    expect(screen.getByText("5 pts")).toBeInTheDocument();
  });

  it("shows no points badge when the task is unsized — never '0 pts'", () => {
    render(<Harness card={{ ...CARD, estimate_points: null }} />);
    expect(screen.queryByText(/pts/)).not.toBeInTheDocument();
  });

  it("badges unmet dependencies", () => {
    render(<Harness unmetDeps={2} />);
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("shows no blocked badge when nothing blocks the task", () => {
    render(<Harness unmetDeps={0} />);
    expect(screen.queryByText(/dependencies not done/)).not.toBeInTheDocument();
  });
});

describe("shortTaskId", () => {
  it("is a UUID prefix — there is no T-231 key to render", () => {
    expect(shortTaskId("0192f4a1-1111-7000-8000-000000000001")).toBe(
      "0192f4a1",
    );
  });
});
