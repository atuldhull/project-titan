import { describe, expect, it } from "vitest";

import { PROTOCOL_VERSION, type ServerFrame } from "@titan/ws-protocol";

import { toRow } from "@/components/overview/activity-feed";

function frame(
  type: string,
  payload: unknown,
  projectId: string | null = null,
): ServerFrame {
  return {
    v: PROTOCOL_VERSION,
    type: type as ServerFrame["type"],
    seq: 7,
    ts: "2026-07-16T12:04:00Z",
    project_id: projectId,
    payload,
  };
}

describe("activity feed row mapping", () => {
  it("reads the real event + project from an org frame's PAYLOAD", () => {
    // The backend publishes activity.created with payload {event, project_id}
    // and leaves the envelope's project_id null. Reading the envelope would make
    // every row the literal string "activity.created", with no link.
    const row = toRow(
      frame("activity.created", { event: "task.created", project_id: "p1" }),
    );
    expect(row?.type).toBe("task.created");
    expect(row?.projectId).toBe("p1");
  });

  it("falls back to the frame type when the payload is malformed", () => {
    expect(toRow(frame("activity.created", {}))?.type).toBe("activity.created");
    expect(toRow(frame("activity.created", null))?.type).toBe(
      "activity.created",
    );
  });

  it("uses the envelope for per-project frames", () => {
    const row = toRow(frame("task.updated", {}, "p2"));
    expect(row?.type).toBe("task.updated");
    expect(row?.projectId).toBe("p2");
  });

  it("drops token-stream noise", () => {
    expect(toRow(frame("run.output.delta", { delta: "x" }, "p1"))).toBeNull();
  });
});
