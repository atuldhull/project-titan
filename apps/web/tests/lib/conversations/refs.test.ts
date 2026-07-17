/**
 * TITAN-FRT-029's deep links, against the real schema.
 *
 * The AC asks for "link-integrity over all eight message types". These cover
 * all NINE kinds that exist — and assert, per kind, which references are links
 * and which are honestly inert, because only one of FRT-029's four named
 * entities is reachable at all.
 */

import { describe, expect, it } from "vitest";

import type { MessageOut } from "@titan/api-client";

import { isBlocked, kindMeta, MESSAGE_KINDS } from "@/lib/conversations/kinds";
import { parseArtifactRef, refsOf, summarize } from "@/lib/conversations/refs";

const PID = "11111111-1111-7111-8111-111111111111";
const TASK = "22222222-2222-7222-8222-222222222222";
const OTHER = "33333333-3333-7333-8333-333333333333";

function message(over: Partial<MessageOut> = {}): MessageOut {
  return {
    id: "44444444-4444-7444-8444-444444444444",
    project_id: PID,
    thread_id: "55555555-5555-7555-8555-555555555555",
    kind: "progress_report",
    sender_agent_id: null,
    sender_user_id: null,
    body: {},
    task_id: null,
    schema_version: "1.0",
    correlation_id: null,
    from_agent: null,
    to_agent: null,
    priority: "normal",
    requires_ack: false,
    created_at: "2026-07-01T00:00:00Z",
    version: 1,
    ...over,
  } as MessageOut;
}

describe("refsOf: the task link is the only real one", () => {
  it("links a message's task_id — the one typed, FK'd reference", () => {
    const refs = refsOf(message({ task_id: TASK }), PID);
    expect(refs).toHaveLength(1);
    expect(refs[0].kind).toBe("task");
    expect(refs[0].href).toBe(`/projects/${PID}/board?task=${TASK}`);
  });

  it("links review_request.context_task_id", () => {
    const refs = refsOf(
      message({
        kind: "review_request",
        body: { context_task_id: TASK, artifact_ref: "pr:42" },
      }),
      PID,
    );
    expect(refs.find((r) => r.kind === "task")?.href).toBe(
      `/projects/${PID}/board?task=${TASK}`,
    );
  });

  it("does not repeat the task when context_task_id equals task_id", () => {
    const refs = refsOf(
      message({
        kind: "review_request",
        task_id: TASK,
        body: { context_task_id: TASK },
      }),
      PID,
    );
    expect(refs.filter((r) => r.kind === "task")).toHaveLength(1);
  });

  it("links each dependency of a task_assignment", () => {
    const refs = refsOf(
      message({ kind: "task_assignment", body: { depends_on: [TASK, OTHER] } }),
      PID,
    );
    expect(refs.filter((r) => r.kind === "task").map((r) => r.href)).toEqual([
      `/projects/${PID}/board?task=${TASK}`,
      `/projects/${PID}/board?task=${OTHER}`,
    ]);
  });
});

describe("refsOf: what FRT-029 asks for that cannot be linked", () => {
  it("names a PR but does not pretend to open one", () => {
    // artifact_ref is a free string ("pr:42") and there is no PR route.
    const refs = refsOf(
      message({ kind: "review_request", body: { artifact_ref: "pr:42" } }),
      PID,
    );
    expect(refs[0]).toMatchObject({ kind: "pr", label: "PR #42", href: null });
    expect(refs[0].reason).toBeTruthy();
  });

  it("does not link a meeting's decisions to the ADR panel", () => {
    // THE trap: meeting_summary.decisions[] holds decision_log MESSAGE uuids,
    // which are not rows in the `decisions` table the ADR panel renders.
    // Linking them would assert an identity the data does not have.
    const refs = refsOf(
      message({ kind: "meeting_summary", body: { decisions: [OTHER] } }),
      PID,
    );
    expect(refs[0].kind).toBe("message");
    expect(refs[0].href).toBeNull();
    expect(refs[0].reason).toContain("not an ADR");
  });

  it("names a story and a conflict without inventing routes for them", () => {
    expect(
      refsOf(
        message({ kind: "task_assignment", body: { story_id: OTHER } }),
        PID,
      )[0],
    ).toMatchObject({
      kind: "story",
      href: null,
    });
    expect(
      refsOf(
        message({ kind: "conflict_resolution", body: { conflict_id: OTHER } }),
        PID,
      )[0],
    ).toMatchObject({ kind: "conflict", href: null });
  });

  it("finds no deployment reference in any kind, because none exists", () => {
    // FRT-029 names deployment as a link target. No payload in Vol 2 §6.2 has a
    // field for one, so there is nothing to extract and nothing to link.
    for (const kind of MESSAGE_KINDS) {
      const refs = refsOf(message({ kind, body: { deployment: "prod" } }), PID);
      expect(refs.some((r) => r.label.toLowerCase().includes("deploy"))).toBe(
        false,
      );
    }
  });
});

describe("refsOf: bodies are untrusted free JSON", () => {
  it("survives a body that is not an object", () => {
    // Every REST body validator is `_permissive`, so this really can happen.
    for (const body of [null, "a string", 42, []]) {
      expect(refsOf(message({ body: body as never }), PID)).toEqual([]);
    }
  });

  it("ignores fields of the wrong type rather than rendering junk", () => {
    const refs = refsOf(
      message({
        kind: "task_assignment",
        body: { story_id: 42, depends_on: "not-an-array", context_task_id: {} },
      }),
      PID,
    );
    expect(refs).toEqual([]);
  });

  it("ignores a non-uuid where a uuid is expected", () => {
    expect(
      refsOf(message({ body: { context_task_id: "../../etc/passwd" } }), PID),
    ).toEqual([]);
  });

  it("skips non-uuid members of a depends_on array", () => {
    const refs = refsOf(
      message({ body: { depends_on: [TASK, "junk", null] } }),
      PID,
    );
    expect(refs).toHaveLength(1);
  });

  it("has nothing to say about an empty body, for every kind", () => {
    for (const kind of MESSAGE_KINDS) {
      expect(refsOf(message({ kind, body: {} }), PID)).toEqual([]);
    }
  });
});

describe("parseArtifactRef", () => {
  it("recognises the pr: scheme", () => {
    expect(parseArtifactRef("pr:42")).toMatchObject({
      kind: "pr",
      label: "PR #42",
    });
  });

  it("keeps an unknown scheme intact rather than guessing", () => {
    expect(parseArtifactRef("branch:feat/orders")).toMatchObject({
      kind: "artifact",
      label: "branch:feat/orders",
      href: null,
    });
  });

  it("does not treat a bare `pr:` as a PR", () => {
    expect(parseArtifactRef("pr:").kind).toBe("artifact");
  });
});

describe("summarize", () => {
  it("prefers body.action, because the log is lossy", () => {
    // start/block/merge/fail all arrive as kind `progress_report`; only
    // body.action says which.
    expect(
      summarize(message({ body: { action: "start", summary: "ignored" } })),
    ).toBe("start");
  });

  it("falls back through the fields the payloads really name", () => {
    expect(summarize(message({ body: { summary: "Sprint 3 status" } }))).toBe(
      "Sprint 3 status",
    );
    expect(summarize(message({ body: { question: "Sync or async?" } }))).toBe(
      "Sync or async?",
    );
  });

  it("is null when there is nothing to say", () => {
    expect(summarize(message({ body: {} }))).toBeNull();
    expect(summarize(message({ body: { action: "   " } }))).toBeNull();
    expect(summarize(message({ body: { action: 42 } }))).toBeNull();
  });
});

describe("kinds", () => {
  it("labels every kind that exists", () => {
    for (const kind of MESSAGE_KINDS) {
      expect(kindMeta(kind).label).toMatch(/\S/);
    }
  });

  it("includes architecture_discussion and human, which §6.6's list omits", () => {
    expect(MESSAGE_KINDS).toContain("architecture_discussion");
    expect(MESSAGE_KINDS).toContain("human");
  });

  it("has no `blocker` kind, because there is no such type", () => {
    // §6.6 lists BLOCKER as one of the eight. It is a FIELD on progress_report.
    expect(MESSAGE_KINDS).not.toContain("blocker");
  });

  it("renders an unknown kind rather than crashing", () => {
    // messages.kind is free text with no CHECK constraint.
    expect(kindMeta("something_new").label).toBe("something_new");
  });
});

describe("isBlocked: §6.6's BLOCKER, as the data really expresses it", () => {
  it("is true for a progress_report with status blocked", () => {
    expect(isBlocked("progress_report", { status: "blocked" })).toBe(true);
  });

  it("is true when it carries blockers, whatever the status says", () => {
    expect(
      isBlocked("progress_report", { blockers: [{ description: "no creds" }] }),
    ).toBe(true);
  });

  it("is false for a healthy progress report", () => {
    expect(
      isBlocked("progress_report", { status: "in_progress", blockers: [] }),
    ).toBe(false);
  });

  it("is false for other kinds and junk bodies", () => {
    expect(isBlocked("bug_report", { status: "blocked" })).toBe(false);
    expect(isBlocked("progress_report", null)).toBe(false);
    expect(isBlocked("progress_report", "blocked")).toBe(false);
  });
});
