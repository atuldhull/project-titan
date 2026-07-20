/**
 * The feed's pure decisions: ordering, dedup, the at-bottom rule, and the token
 * stream reducer.
 */

import { describe, expect, it } from "vitest";

import type { MessageOut } from "@titan/api-client";

import {
  announceArrivals,
  AT_BOTTOM_THRESHOLD_PX,
  filterByKind,
  isAtBottom,
  isBehindModal,
  liveStreams,
  orderOldestFirst,
  pruneStreams,
  reduceStreams,
  scrollTopAfterPrepend,
  type Streams,
} from "@/lib/conversations/feed";

function message(
  id: string,
  created_at: string,
  kind = "progress_report",
): MessageOut {
  return { id, created_at, kind, body: {} } as MessageOut;
}

describe("orderOldestFirst", () => {
  it("flips the API's newest-first pages into reading order", () => {
    // The API sorts -created_at (page 2 is OLDER than page 1); a chat reads the
    // other way.
    const pages = [
      {
        items: [
          message("c", "2026-07-01T00:00:03Z"),
          message("b", "2026-07-01T00:00:02Z"),
        ],
      },
      { items: [message("a", "2026-07-01T00:00:01Z")] },
    ];
    expect(orderOldestFirst(pages).map((m) => m.id)).toEqual(["a", "b", "c"]);
  });

  it("dedups a message delivered twice", () => {
    // A refetch racing a message.created frame really does deliver both.
    const pages = [
      { items: [message("a", "2026-07-01T00:00:01Z")] },
      { items: [message("a", "2026-07-01T00:00:01Z")] },
    ];
    expect(orderOldestFirst(pages)).toHaveLength(1);
  });

  it("breaks a created_at tie by id, so the order is stable", () => {
    const pages = [
      {
        items: [
          message(
            "22222222-0000-7000-8000-000000000000",
            "2026-07-01T00:00:01Z",
          ),
          message(
            "11111111-0000-7000-8000-000000000000",
            "2026-07-01T00:00:01Z",
          ),
        ],
      },
    ];
    const ids = orderOldestFirst(pages).map((m) => m.id);
    expect(ids[0].startsWith("1111")).toBe(true);
  });

  it("handles no pages and empty pages", () => {
    expect(orderOldestFirst([])).toEqual([]);
    expect(orderOldestFirst([{ items: [] }])).toEqual([]);
  });
});

describe("isAtBottom", () => {
  it("is true when pinned to the bottom", () => {
    expect(
      isAtBottom({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 }),
    ).toBe(true);
  });

  it("is false once the reader has scrolled up", () => {
    // The pill exists so this reader is not yanked away from what they are
    // reading.
    expect(
      isAtBottom({ scrollTop: 200, scrollHeight: 1000, clientHeight: 100 }),
    ).toBe(false);
  });

  it("tolerates sub-pixel rounding rather than flipping the pill on", () => {
    expect(
      isAtBottom({
        scrollTop: 900 - AT_BOTTOM_THRESHOLD_PX,
        scrollHeight: 1000,
        clientHeight: 100,
      }),
    ).toBe(true);
    expect(
      isAtBottom({
        scrollTop: 900 - AT_BOTTOM_THRESHOLD_PX - 1,
        scrollHeight: 1000,
        clientHeight: 100,
      }),
    ).toBe(false);
  });

  it("is true for a feed shorter than its container", () => {
    expect(
      isAtBottom({ scrollTop: 0, scrollHeight: 50, clientHeight: 100 }),
    ).toBe(true);
  });
});

describe("isBehindModal", () => {
  it("is true when the screen sits inside an aria-hidden subtree", () => {
    // What an open Radix dialog/sheet/dropdown does to the rest of the page.
    // A window-level shortcut that navigates MUST NOT fire underneath one —
    // Phase 60 shipped exactly that bug with a Tab handler.
    const page = document.createElement("div");
    page.setAttribute("aria-hidden", "true");
    const screen = document.createElement("div");
    page.appendChild(screen);
    document.body.appendChild(page);
    expect(isBehindModal(screen)).toBe(true);
    page.remove();
  });

  it("is false on an ordinary page", () => {
    const screen = document.createElement("div");
    document.body.appendChild(screen);
    expect(isBehindModal(screen)).toBe(false);
    screen.remove();
  });

  it("is false for nothing at all", () => {
    expect(isBehindModal(null)).toBe(false);
  });
});

describe("scrollTopAfterPrepend", () => {
  it("holds the reader's place when history lands above them", () => {
    // The classic reverse-infinite-scroll bug: 400px of older messages are
    // prepended, so the message the reader was looking at is now 400px further
    // down. Keeping scrollTop at 120 would show them something else entirely —
    // near the top, which instantly asks for another page, and again, and again.
    expect(
      scrollTopAfterPrepend({ scrollTop: 120, scrollHeight: 1000 }, 1400),
    ).toBe(520);
  });

  it("leaves the position alone when nothing was added", () => {
    expect(
      scrollTopAfterPrepend({ scrollTop: 120, scrollHeight: 1000 }, 1000),
    ).toBe(120);
  });

  it("does not move the reader when the content SHRANK", () => {
    // e.g. a filter was applied while a page was in flight. Adding a negative
    // delta would scroll them backwards for no reason.
    expect(
      scrollTopAfterPrepend({ scrollTop: 120, scrollHeight: 1000 }, 600),
    ).toBe(120);
  });
});

describe("filterByKind", () => {
  const messages = [
    message("a", "t1", "bug_report"),
    message("b", "t2", "decision_log"),
  ];

  it("shows everything when nothing is selected", () => {
    expect(filterByKind(messages, new Set())).toHaveLength(2);
  });

  it("narrows to the selected kinds", () => {
    expect(
      filterByKind(messages, new Set(["bug_report"])).map((m) => m.id),
    ).toEqual(["a"]);
  });
});

describe("reduceStreams", () => {
  const empty: Streams = new Map();

  it("accumulates deltas for a run", () => {
    let s = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: "a1",
      delta: "Hel",
    });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r1",
      agentId: "a1",
      delta: "lo",
    });
    expect(s.get("r1")?.text).toBe("Hello");
  });

  it("keys by RUN, because a delta carries no thread or message id", () => {
    // The honest limit of the only streaming frame that exists: a bubble can be
    // attributed to a run and its agent, never to a message.
    let s = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: "a1",
      delta: "x",
    });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r2",
      agentId: "a2",
      delta: "y",
    });
    expect([...s.keys()]).toEqual(["r1", "r2"]);
  });

  it("learns the agent id from a later delta", () => {
    // agent_id is optional on the frame — the fan-out only copies it if present.
    let s = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "a",
    });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r1",
      agentId: "a1",
      delta: "b",
    });
    expect(s.get("r1")?.agentId).toBe("a1");
  });

  it("marks a stream done on run.finished", () => {
    let s = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "x",
    });
    s = reduceStreams(s, { type: "finished", runId: "r1" });
    expect(s.get("r1")?.done).toBe(true);
  });

  it("does not invent a bubble for a run that never streamed", () => {
    // Almost no run emits deltas — only the CEO intake flow does. A finished
    // frame for an ordinary run must not leave an empty bubble behind.
    const s = reduceStreams(empty, { type: "finished", runId: "r1" });
    expect(s.size).toBe(0);
    expect(s).toBe(empty);
  });

  it("does not truncate when a delta arrives after finished", () => {
    // run.output.delta is UNBUFFERED and run.finished is BUFFERED, so they are
    // not ordered relative to each other. Dropping late tokens would silently
    // lose the tail of a reply.
    let s = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "Hel",
    });
    s = reduceStreams(s, { type: "finished", runId: "r1" });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "lo",
    });
    expect(s.get("r1")?.text).toBe("Hello");
    expect(s.get("r1")?.done).toBe(false);
  });

  it("never mutates the map it was given", () => {
    const before = reduceStreams(empty, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "a",
    });
    const after = reduceStreams(before, {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "b",
    });
    expect(before.get("r1")?.text).toBe("a");
    expect(after.get("r1")?.text).toBe("ab");
  });
});

describe("liveStreams / pruneStreams", () => {
  it("renders only unfinished streams that have said something", () => {
    let s = reduceStreams(new Map(), {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "hi",
    });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r2",
      agentId: null,
      delta: "yo",
    });
    s = reduceStreams(s, { type: "finished", runId: "r2" });
    expect(liveStreams(s).map((x) => x.runId)).toEqual(["r1"]);
  });

  it("drops finished streams and keeps live ones", () => {
    let s = reduceStreams(new Map(), {
      type: "delta",
      runId: "r1",
      agentId: null,
      delta: "hi",
    });
    s = reduceStreams(s, {
      type: "delta",
      runId: "r2",
      agentId: null,
      delta: "yo",
    });
    s = reduceStreams(s, { type: "finished", runId: "r2" });
    expect([...pruneStreams(s).keys()]).toEqual(["r1"]);
  });
});

describe("announceArrivals", () => {
  const describeOne = (m: MessageOut) => `sender, ${m.kind}`;
  const feed = [
    message("a", "2026-07-01T00:00:01Z"),
    message("b", "2026-07-01T00:00:02Z"),
    message("c", "2026-07-01T00:00:03Z"),
  ];

  it("says nothing on the first paint, but records where it started", () => {
    // Landing on a channel with 200 messages must not read 200 messages aloud.
    const result = announceArrivals(feed, null, describeOne);
    expect(result).toEqual({ text: "", lastId: "c" });
  });

  it("says nothing when the newest message has already been spoken", () => {
    expect(announceArrivals(feed, "c", describeOne)).toEqual({
      text: "",
      lastId: "c",
    });
  });

  it("announces a single arrival", () => {
    const result = announceArrivals(feed, "b", describeOne);
    expect(result?.text).toBe("New message: sender, progress_report");
    expect(result?.lastId).toBe("c");
  });

  it("collapses a burst into one utterance with a count", () => {
    // An agent standup lands several at once; five queued utterances the reader
    // cannot interrupt is worse than one.
    const result = announceArrivals(feed, "a", describeOne);
    expect(result?.text).toBe(
      "2 new messages. Latest: sender, progress_report",
    );
  });

  it("is silent about a PREPEND — history paging is not an arrival", () => {
    const older = message("z", "2026-07-01T00:00:00Z");
    expect(announceArrivals([older, ...feed], "c", describeOne)).toEqual({
      text: "",
      lastId: "c",
    });
  });

  it("treats a vanished anchor as ONE arrival, not the whole feed", () => {
    // The spoken message was filtered away by a kind chip. `messages.length`
    // here would announce "3 new messages" for an action the reader just took.
    const result = announceArrivals(feed, "gone", describeOne);
    expect(result?.text).toBe("New message: sender, progress_report");
  });

  it("returns null for an empty feed rather than seeding on nothing", () => {
    expect(announceArrivals([], null, describeOne)).toBeNull();
  });
});
