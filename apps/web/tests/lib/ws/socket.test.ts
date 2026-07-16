import { describe, expect, it } from "vitest";

import { flush, frame, makeHarness } from "./harness";

async function connectAndOpen(h: ReturnType<typeof makeHarness>) {
  h.client.connect();
  await flush();
  h.socket().emitOpen();
}

describe("WsClient connection lifecycle", () => {
  it("goes connecting → live on open and fetches a ticket per connect", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    expect(h.client.status).toBe("live");
    expect(h.statuses).toContain("live");
    expect(h.ticketCount()).toBe(1);
  });

  it("shows live → reconnecting → live across a drop/restore cycle", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    expect(h.client.status).toBe("live");

    h.socket().emitClose();
    expect(h.client.status).toBe("reconnecting");

    h.scheduler.advance(200); // fire the backoff timer → reconnect
    await flush();
    h.socket().emitOpen();
    expect(h.client.status).toBe("live");
    expect(h.ticketCount()).toBe(2); // fresh ticket on reconnect
  });

  it("degrades to offline after 60 s of continuous reconnecting", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitClose();
    // Keep failing reconnects past the degraded threshold (600 ms in the harness).
    for (let i = 0; i < 12; i++) {
      h.scheduler.advance(200);
      await flush();
      // never emit open — each attempt makes a socket that then drops
      if (h.socket() && !h.socket().closed) {
        h.socket().emitClose();
      }
    }
    expect(h.client.status).toBe("offline");
    expect(h.statuses).toContain("offline");
  });

  it("manual retry reconnects immediately and resets backoff", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitClose();
    const before = h.ticketCount();
    h.client.retry();
    await flush();
    expect(h.ticketCount()).toBe(before + 1);
  });
});

describe("WsClient heartbeat and liveness", () => {
  it("replies pong to a server ping", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("ping"));
    expect(h.sent).toContain(JSON.stringify({ type: "pong" }));
  });

  it("reconnects when no frame arrives within the liveness window", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    const first = h.socket();
    h.scheduler.advance(1000); // liveness timeout → close
    expect(first.closed).toBe(true);
    expect(h.client.status).toBe("reconnecting");
  });
});

describe("WsClient subscriptions", () => {
  it("ref-counts: two subscribers → one subscribe, last unsubscribe → one unsubscribe", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.client.subscribe("p1", ["tasks"]);
    h.client.subscribe("p1", ["tasks"]);
    const subs = h.sent.filter((s) => s.includes('"subscribe"'));
    expect(subs).toHaveLength(1);

    h.client.unsubscribe("p1", ["tasks"]);
    expect(h.sent.filter((s) => s.includes('"unsubscribe"'))).toHaveLength(0);
    h.client.unsubscribe("p1", ["tasks"]);
    expect(h.sent.filter((s) => s.includes('"unsubscribe"'))).toHaveLength(1);
  });

  it("re-subscribes every active topic on reconnect, replaying from last_seq", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.client.subscribe("p1", ["tasks"]);
    // apply a frame so last_seq advances to 5
    h.socket().emitMessage(frame("task.updated", { seq: 5, projectId: "p1" }));

    h.socket().emitClose();
    h.scheduler.advance(200);
    await flush();
    h.socket().emitOpen();

    const resubscribe = h.sent.filter((s) => s.includes('"subscribe"')).at(-1);
    expect(resubscribe).toContain('"last_seq":5');
    expect(h.reconciles).toContainEqual({
      type: "reconnect",
      projectIds: ["p1"],
    });
  });
});

describe("WsClient seq gap + dedupe (TITAN-FRT-019)", () => {
  it("delivers in-order frames without reconciling", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.client.subscribe("p1", ["tasks"]);
    h.socket().emitMessage(frame("task.updated", { seq: 1, projectId: "p1" }));
    h.socket().emitMessage(frame("task.updated", { seq: 2, projectId: "p1" }));
    expect(h.frames).toHaveLength(2);
    expect(h.reconciles.filter((r) => r.type === "gap")).toHaveLength(0);
  });

  it("detects a seq gap and reconciles the project", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("task.updated", { seq: 1, projectId: "p1" }));
    h.socket().emitMessage(frame("task.updated", { seq: 4, projectId: "p1" }));
    expect(h.reconciles).toContainEqual({ type: "gap", projectId: "p1" });
  });

  it("drops duplicate/replayed frames (no double apply)", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("task.updated", { seq: 3, projectId: "p1" }));
    h.socket().emitMessage(frame("task.updated", { seq: 3, projectId: "p1" }));
    h.socket().emitMessage(frame("task.updated", { seq: 2, projectId: "p1" }));
    expect(h.frames).toHaveLength(1);
  });

  // The server sends sys.resync with seq 0 — the harness mirrors the real wire.
  it("routes a sys.resync to a resync reconcile", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("sys.resync", { seq: 0, projectId: "p1" }));
    expect(h.reconciles).toContainEqual({ type: "resync", projectId: "p1" });
  });

  it("sys.resync clears the cursor instead of rewinding it to 0", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("task.updated", { seq: 5, projectId: "p1" }));
    h.socket().emitMessage(frame("sys.resync", { seq: 0, projectId: "p1" }));
    h.reconciles.length = 0;
    // Re-baselines off the next live frame: no spurious gap → no second refetch.
    h.socket().emitMessage(
      frame("task.updated", { seq: 900, projectId: "p1" }),
    );
    expect(h.reconciles.filter((r) => r.type === "gap")).toHaveLength(0);
  });

  it("recovers when the server's seq counter restarts at 1", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(
      frame("task.updated", { seq: 500, projectId: "p1" }),
    );
    h.frames.length = 0;
    h.reconciles.length = 0;
    // Redis lost the seq key and the server streams from 1 again. Treating this
    // as a duplicate would freeze the project's feed forever.
    h.socket().emitMessage(frame("task.updated", { seq: 1, projectId: "p1" }));
    expect(h.frames).toHaveLength(1);
    expect(h.reconciles).toContainEqual({ type: "gap", projectId: "p1" });
  });
});

describe("WsClient health gating", () => {
  it("treats accept-then-close (connection cap) as unhealthy and degrades", async () => {
    const h = makeHarness();
    h.client.connect();
    await flush();
    // The server accepts BEFORE enforcing the cap, then sends an error frame and
    // closes. If `onopen` reset the backoff, this would loop at ~1 Hz forever and
    // never reach offline.
    for (let i = 0; i < 10; i++) {
      h.socket().emitOpen();
      h.socket().emitMessage(
        frame("error", { payload: { code: "connection_cap" } }),
      );
      h.scheduler.advance(2000);
      await flush();
    }
    expect(h.client.status).toBe("offline");
  });

  it("treats a real frame as healthy and resets the backoff clock", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(frame("ping"));
    h.socket().emitClose();
    // Healthy before the drop → the degradation clock restarts from now.
    expect(h.client.status).toBe("reconnecting");
  });

  it("never sends while the socket is still connecting", async () => {
    const h = makeHarness();
    h.client.connect();
    await flush();
    // The socket exists but has not opened: send() would throw and orphan it.
    h.client.subscribe("p1", ["tasks"]);
    expect(h.sent).toHaveLength(0);
    h.socket().emitOpen();
    expect(h.sent.filter((s) => s.includes('"subscribe"'))).toHaveLength(1);
  });

  it("delivers project-less frames (notifications) without seq tracking", async () => {
    const h = makeHarness();
    await connectAndOpen(h);
    h.socket().emitMessage(
      frame("notification.created", { seq: 0, projectId: null }),
    );
    expect(h.frames).toHaveLength(1);
    expect(h.reconciles).toHaveLength(0);
  });
});
