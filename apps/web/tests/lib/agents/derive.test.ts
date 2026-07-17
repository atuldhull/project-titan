/**
 * The §6.5 grid's real logic: an agent's state, task, model and throughput,
 * derived from its runs.
 *
 * These tests double as the record of WHY the derivation reads runs and not the
 * agents table — several assert the absence the spec assumes away.
 */

import { describe, expect, it } from "vitest";

import type { AgentOut } from "@titan/api-client";

import type { ProjectRun } from "@/lib/agents/types";

import {
  activeRun,
  agentState,
  currentTaskId,
  elapsedMs,
  lastFailure,
  latestRun,
  modelInUse,
  rollup,
  runsByAgent,
  throughput,
} from "@/lib/agents/derive";

const AGENT_ID = "aaaaaaaa-0000-7000-8000-000000000001";

function agent(over: Partial<AgentOut> = {}): AgentOut {
  return {
    id: AGENT_ID,
    project_id: "pppppppp-0000-7000-8000-000000000001",
    role: "backend",
    roster_id: "eng_backend",
    name: "BE-1",
    status: "idle",
    model_binding: { provider: "anthropic", model: "claude-opus-4-8" },
    config: {},
    current_task_id: null,
    created_at: "2026-07-01T00:00:00Z",
    version: 1,
    ...over,
  } as AgentOut;
}

let seq = 0;
function run(over: Partial<ProjectRun> = {}): ProjectRun {
  seq += 1;
  return {
    id: `rrrrrrrr-0000-7000-8000-${String(seq).padStart(12, "0")}`,
    task_id: "tttttttt-0000-7000-8000-000000000001",
    agent_id: AGENT_ID,
    project_id: "pppppppp-0000-7000-8000-000000000001",
    attempt: 1,
    status: "queued",
    input: {},
    output_summary: null,
    error: null,
    provider: null,
    model: null,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: "0",
    started_at: null,
    finished_at: null,
    created_at: `2026-07-01T00:00:${String(seq).padStart(2, "0")}Z`,
    version: 1,
    ...over,
  } as ProjectRun;
}

describe("agentState", () => {
  it("is idle with no runs at all", () => {
    expect(agentState(agent(), [])).toBe("idle");
  });

  it("is working while a run is running", () => {
    expect(agentState(agent(), [run({ status: "running" })])).toBe("working");
  });

  it("counts a QUEUED run as working, matching the DB's own notion of active", () => {
    // uq_runs_single_active_owner and idx_runs_status both partial on
    // status IN ('queued','running'): a queued run is work this agent owns.
    expect(agentState(agent(), [run({ status: "queued" })])).toBe("working");
  });

  it("is error when the newest run failed", () => {
    expect(
      agentState(agent(), [run({ status: "failed", error: "LLM timeout" })]),
    ).toBe("error");
  });

  it("treats a timed-out run as an error too", () => {
    expect(agentState(agent(), [run({ status: "timed_out" })])).toBe("error");
  });

  it("is working, not error, when new work followed a failure", () => {
    // An agent that failed and has already been handed a fresh run is working.
    // Ordering by created_at is what makes this right; a naive `.some(failed)`
    // would pin the card to error forever.
    const runs = [run({ status: "failed" }), run({ status: "running" })];
    expect(agentState(agent(), runs)).toBe("working");
  });

  it("is paused regardless of any run still queued against it", () => {
    // `paused` is the only status a human writes, and it outranks the runs.
    expect(
      agentState(agent({ status: "paused" }), [run({ status: "running" })]),
    ).toBe("paused");
  });

  it("is idle after a successful run, not working", () => {
    expect(agentState(agent(), [run({ status: "succeeded" })])).toBe("idle");
  });

  it("never reports `busy` or `error` from agents.status, which nothing writes", () => {
    // The AgentStatus Literal names busy|error, but the ONLY writer is
    // transition_status via pause/resume, so those values never occur. If a
    // future backend starts writing them, this test should be revisited — it
    // pins today's truth, not a wish.
    expect(agentState(agent({ status: "busy" }), [])).toBe("idle");
  });
});

describe("currentTaskId / activeRun", () => {
  it("comes from the run, because agents.current_task_id is always null", () => {
    const a = agent({ current_task_id: null });
    const r = run({ status: "running", task_id: "task-42" });
    expect(a.current_task_id).toBeNull();
    expect(currentTaskId([r])).toBe("task-42");
  });

  it("is null when nothing is in flight", () => {
    expect(currentTaskId([run({ status: "succeeded" })])).toBeNull();
    expect(activeRun([run({ status: "cancelled" })])).toBeNull();
  });

  it("picks the newest active run when several exist", () => {
    const older = run({ status: "running", task_id: "old" });
    const newer = run({ status: "running", task_id: "new" });
    expect(activeRun([older, newer])?.task_id).toBe("new");
    // ...and does not depend on the caller's array order.
    expect(activeRun([newer, older])?.task_id).toBe("new");
  });
});

describe("elapsedMs", () => {
  const NOW = Date.parse("2026-07-01T00:10:00Z");

  it("measures from the run's started_at", () => {
    const r = run({ status: "running", started_at: "2026-07-01T00:08:00Z" });
    expect(elapsedMs([r], NOW)).toBe(120_000);
  });

  it("is null for a queued run, which has not started", () => {
    expect(
      elapsedMs([run({ status: "queued", started_at: null })], NOW),
    ).toBeNull();
  });

  it("is null when nothing is active", () => {
    expect(
      elapsedMs(
        [run({ status: "succeeded", started_at: "2026-07-01T00:00:00Z" })],
        NOW,
      ),
    ).toBeNull();
  });

  it("clamps a clock skew to zero rather than rendering a negative age", () => {
    const r = run({ status: "running", started_at: "2026-07-01T00:20:00Z" });
    expect(elapsedMs([r], NOW)).toBe(0);
  });

  it("is null for an unparseable timestamp instead of NaN", () => {
    const r = run({ status: "running", started_at: "not-a-date" });
    expect(elapsedMs([r], NOW)).toBeNull();
  });
});

describe("throughput", () => {
  it("averages a completed run's tokens over its wall time", () => {
    const r = run({
      status: "succeeded",
      tokens_out: 1280,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:02Z",
    });
    expect(throughput([r])?.tokensPerSecond).toBe(640);
  });

  it("is null for an in-flight run — tokens are only written at finalize", () => {
    // THE point of this function's design. A running run's tokens_out is still
    // its server default 0, so a live rate would render a confident 0 tok/s on
    // every working card.
    const r = run({
      status: "running",
      tokens_out: 0,
      started_at: "2026-07-01T00:00:00Z",
    });
    expect(throughput([r])).toBeNull();
  });

  it("is null when a completed run recorded no tokens", () => {
    const r = run({
      status: "succeeded",
      tokens_out: 0,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:02Z",
    });
    expect(throughput([r])).toBeNull();
  });

  it("is null rather than Infinity for a sub-millisecond run", () => {
    const r = run({
      status: "succeeded",
      tokens_out: 10,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:00Z",
    });
    expect(throughput([r])).toBeNull();
  });

  it("reports the newest completed run, not the oldest", () => {
    const old = run({
      status: "succeeded",
      tokens_out: 100,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:01Z",
    });
    const recent = run({
      status: "succeeded",
      tokens_out: 500,
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:00:01Z",
    });
    expect(throughput([old, recent])?.runId).toBe(recent.id);
  });
});

describe("modelInUse", () => {
  it("prefers what the run really used over the configured binding", () => {
    const r = run({
      status: "running",
      provider: "ollama",
      model: "qwen2.5-coder:7b",
    });
    expect(modelInUse(agent(), [r])).toEqual({
      provider: "ollama",
      model: "qwen2.5-coder:7b",
      actual: true,
    });
  });

  it("falls back to the binding, marked as not-actual, before anything has run", () => {
    expect(modelInUse(agent(), [])).toEqual({
      provider: "anthropic",
      model: "claude-opus-4-8",
      actual: false,
    });
  });

  it("keeps showing the real model while a new run is in flight", () => {
    // THE regression this pins. `insert_run` copies provider/model from a
    // RunCreate that defaults both to None; only `finalize_run` stamps what was
    // really used. So the NEWEST run of a busy agent carries nulls — and
    // preferring it labelled every working agent "(configured)" while a pile of
    // finished runs recorded the truth right behind it.
    const finished = run({
      status: "succeeded",
      provider: "ollama",
      model: "qwen2.5-coder:7b",
    });
    const inFlight = run({ status: "running", provider: null, model: null });
    expect(modelInUse(agent(), [finished, inFlight])).toEqual({
      provider: "ollama",
      model: "qwen2.5-coder:7b",
      actual: true,
    });
  });

  it("survives a model_binding that is not the shape we expect", () => {
    // model_binding is free-form JSONB — nothing constrains its keys.
    const a = agent({ model_binding: { nested: { model: "x" } } });
    expect(modelInUse(a, [])).toEqual({
      provider: null,
      model: null,
      actual: false,
    });
  });

  it("uses the last run's model when idle, so the chip does not go blank", () => {
    const r = run({ status: "succeeded", provider: "ollama", model: "qwen" });
    expect(modelInUse(agent(), [r]).actual).toBe(true);
  });
});

describe("lastFailure", () => {
  it("surfaces the newest failure's error text", () => {
    const r = run({ status: "failed", error: "LLM timeout" });
    expect(lastFailure([r])?.error).toBe("LLM timeout");
  });

  it("is null once newer work has started", () => {
    expect(
      lastFailure([run({ status: "failed" }), run({ status: "running" })]),
    ).toBeNull();
  });

  it("is null whenever the agent has live work, even if the failure is NEWER", () => {
    // `lastFailure` and `agentState` must obey the same rule or the card says
    // two things at once. An agent owning a long build (run A, older, still
    // running) whose newer short run B failed: the chip reads "working" from
    // the active run, so red error text underneath would contradict it.
    const longBuild = run({ status: "running" });
    const shortFailure = run({ status: "failed", error: "LLM timeout" });
    expect(agentState(agent(), [longBuild, shortFailure])).toBe("working");
    expect(lastFailure([longBuild, shortFailure])).toBeNull();
  });
});

describe("rollup", () => {
  it("counts each state, including the zeroes", () => {
    expect(rollup(["working", "working", "idle", "error"])).toEqual({
      working: 2,
      idle: 1,
      error: 1,
      paused: 0,
    });
  });

  it("is all zeroes for no agents", () => {
    expect(rollup([])).toEqual({ working: 0, idle: 0, error: 0, paused: 0 });
  });
});

describe("runsByAgent", () => {
  it("buckets one project-wide fetch into per-agent lists", () => {
    const mine = run({ agent_id: "a1" });
    const alsoMine = run({ agent_id: "a1" });
    const theirs = run({ agent_id: "a2" });
    const byAgent = runsByAgent([mine, alsoMine, theirs]);
    expect(byAgent.get("a1")).toHaveLength(2);
    expect(byAgent.get("a2")).toHaveLength(1);
    expect(byAgent.get("nobody")).toBeUndefined();
  });

  it("returns an empty map for no runs", () => {
    expect(runsByAgent([]).size).toBe(0);
  });
});

describe("latestRun", () => {
  it("is null for no runs", () => {
    expect(latestRun([])).toBeNull();
  });
});
