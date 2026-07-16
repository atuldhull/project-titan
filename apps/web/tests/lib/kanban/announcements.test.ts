/**
 * What a screen reader says during a keyboard move (TITAN-FRT-025/043).
 *
 * dnd-kit pipes these into its `aria-live` region. They are asserted as pure
 * strings because the interesting content is not that a live region exists — it
 * is whether the words are TRUE: a keyboard user cannot see that a column is
 * dimmed, so "over Done" must not be said when Done would refuse the card.
 */

import { describe, expect, it } from "vitest";

import {
  moveFailedMessage,
  onDragCancelMessage,
  onDragEndMessage,
  onDragOverMessage,
  onDragStartMessage,
} from "@/lib/kanban/announcements";

describe("drag announcements", () => {
  it("says how to drive the move on pick-up", () => {
    const message = onDragStartMessage({
      title: "Payment API",
      fromStatus: "in_progress",
    });
    expect(message).toContain("Picked up Payment API");
    expect(message).toContain("In progress");
    // The keys are not discoverable without sight — say them.
    expect(message).toContain("arrow keys");
    expect(message).toContain("space to drop");
    expect(message).toContain("escape to cancel");
  });

  it("says a column will not accept the card", () => {
    const message = onDragOverMessage({
      title: "Payment API",
      fromStatus: "backlog",
      overStatus: "done",
      allowed: false,
    });
    expect(message).toContain("will not accept it");
    expect(message).not.toContain("Press space to move it there");
  });

  it("offers the drop only where it is legal", () => {
    expect(
      onDragOverMessage({
        title: "Payment API",
        fromStatus: "backlog",
        overStatus: "ready",
        allowed: true,
      }),
    ).toContain("Press space to move it there");
  });

  it("names the origin when the card is back where it started", () => {
    expect(
      onDragOverMessage({
        title: "Payment API",
        fromStatus: "backlog",
        overStatus: "backlog",
        allowed: true,
      }),
    ).toContain("where it started");
  });

  it("confirms a completed move", () => {
    expect(
      onDragEndMessage({
        title: "Payment API",
        fromStatus: "backlog",
        overStatus: "ready",
        allowed: true,
      }),
    ).toBe("Payment API was moved to Ready.");
  });

  it("says the card was returned on an illegal drop", () => {
    const message = onDragEndMessage({
      title: "Payment API",
      fromStatus: "backlog",
      overStatus: "done",
      allowed: false,
    });
    expect(message).toContain("does not accept");
    expect(message).toContain("returned");
  });

  it("says the card was returned when dropped on nothing", () => {
    expect(
      onDragEndMessage({
        title: "Payment API",
        fromStatus: "backlog",
        overStatus: null,
      }),
    ).toBe("Payment API was returned to Backlog.");
  });

  it("confirms a cancel names where the card stays", () => {
    expect(
      onDragCancelMessage({ title: "Payment API", fromStatus: "backlog" }),
    ).toBe("Move cancelled. Payment API stays in Backlog.");
  });

  it("announces a server-side failure and the rollback", () => {
    // The optimistic move already showed the card in the new column; a silent
    // rollback would leave a screen-reader user believing it landed.
    expect(moveFailedMessage("Payment API", "backlog")).toBe(
      "Moving Payment API failed. It was returned to Backlog.",
    );
  });
});
