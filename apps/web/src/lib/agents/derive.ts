/**
 * What an agent is doing, derived from its RUNS (Vol 4 §6.5).
 *
 * §6.5 reads like it describes the `agents` table. It does not, and building
 * the grid on that table would render an empty shell:
 *
 * - `agents.current_task_id` has NO writer anywhere in the backend. It is
 *   selected, it is in `AgentOut`, it is even in the `agent.status` payload —
 *   and it is always NULL. §6.5's "current task link" cannot come from it.
 * - `agents.status` only ever holds `idle` or `paused`. The Literal names
 *   `busy` and `error` too, but the only writer is `transition_status`, called
 *   from exactly two places: `pause_agent` (-> paused) and `resume_agent`
 *   (-> idle). Nothing ever writes `busy`; nothing ever writes `error`.
 * - `agent.status` is likewise emitted from those two call sites only. There is
 *   no heartbeat anywhere in the backend (the 25s `ping` is socket liveness).
 *
 * A RUN carries all of it and is written by the real execution path: agent_id
 * (a NOT NULL FK), task_id, status, provider, model, tokens_in/out, started_at,
 * finished_at and error. So the grid joins agents to their runs, and everything
 * below is a pure function of that pair — which is also what makes it testable,
 * since the cards themselves are React.
 */

import type { AgentOut, RunOut } from "@titan/api-client";

/**
 * §6.5 asks for working | idle | blocked | error | paused.
 *
 * `blocked` is absent on purpose: it has no agent-level meaning in this system.
 * A *task* can be blocked; an agent cannot. Rendering a `blocked` chip would
 * mean inventing a state the backend never expresses.
 */
export type AgentState = "working" | "idle" | "paused" | "error";

/**
 * The statuses the DATABASE itself treats as an in-flight run — the same set as
 * `uq_runs_single_active_owner` and `idx_runs_status`. Taking the definition
 * from the schema rather than inventing one keeps "working" meaning exactly
 * what "this agent owns live work" means to the backend.
 */
const ACTIVE_RUN_STATUSES: readonly string[] = ["queued", "running"];

const TERMINAL_FAILURES: readonly string[] = ["failed", "timed_out"];

/** Runs newest-first. The API already sorts this way; sorting again makes these
 *  functions independent of their caller's ordering. */
function newestFirst<T extends RunOut>(runs: readonly T[]): T[] {
  return [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** The run this agent currently owns, if any. */
export function activeRun<T extends RunOut>(runs: readonly T[]): T | null {
  return (
    newestFirst(runs).find((r) => ACTIVE_RUN_STATUSES.includes(r.status)) ??
    null
  );
}

/** The most recent run regardless of status. */
export function latestRun<T extends RunOut>(runs: readonly T[]): T | null {
  return newestFirst(runs)[0] ?? null;
}

/**
 * The agent's state chip.
 *
 * Precedence is deliberate: `paused` is an explicit human act and outranks
 * everything (a paused agent is not "working" even if a run is still queued
 * against it). Otherwise a live run wins over a stale failure — an agent that
 * failed and has already been given new work is working, not erroring.
 */
export function agentState(
  agent: AgentOut,
  runs: readonly RunOut[],
): AgentState {
  if (agent.status === "paused") {
    return "paused";
  }
  if (activeRun(runs) !== null) {
    return "working";
  }
  const last = latestRun(runs);
  if (last !== null && TERMINAL_FAILURES.includes(last.status)) {
    return "error";
  }
  return "idle";
}

/** How long the agent has been on its current run, in ms. Null when it has not
 *  started (a queued run has no `started_at`) or there is no active run. */
export function elapsedMs(
  runs: readonly RunOut[],
  nowMs: number,
): number | null {
  const run = activeRun(runs);
  if (run?.started_at == null) {
    return null;
  }
  const started = Date.parse(run.started_at);
  if (Number.isNaN(started)) {
    return null;
  }
  return Math.max(0, nowMs - started);
}

export interface Throughput {
  readonly tokensPerSecond: number;
  /** The run it was measured over — this is an average across a COMPLETED run,
   *  never a live rate (see {@link throughput}). */
  readonly runId: string;
}

/**
 * Tokens per second — and it can only ever be the LAST COMPLETED run's average.
 *
 * §6.5's card shows a live "640 tok/s". That is not obtainable here: token
 * counts are written once, by `finalize_run`, when the run reaches a terminal
 * status. An in-flight run's `tokens_out` is still its server default of 0, so
 * dividing it by elapsed time would render a confident, moving `0 tok/s` for
 * every working agent.
 *
 * The one live token signal in the system is the `run.output.delta` frame, and
 * its only producer is the CEO intake flow (gated behind `ceo_intake_enabled`,
 * default off) — no agent turn streams tokens.
 *
 * So this reports the average over the newest finished run, and the UI labels
 * it as such. A number that is real and past beats a number that is live and
 * invented.
 */
export function throughput(runs: readonly RunOut[]): Throughput | null {
  const done = newestFirst(runs).find(
    (r) =>
      r.status === "succeeded" &&
      r.started_at != null &&
      r.finished_at != null &&
      (r.tokens_out ?? 0) > 0,
  );
  if (!done || done.started_at == null || done.finished_at == null) {
    return null;
  }
  const seconds =
    (Date.parse(done.finished_at) - Date.parse(done.started_at)) / 1000;
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return null;
  }
  return { tokensPerSecond: (done.tokens_out ?? 0) / seconds, runId: done.id };
}

export interface ModelInUse {
  readonly provider: string | null;
  readonly model: string | null;
  /** True when this is what a run really used; false when it is only the
   *  agent's configured binding and nothing has run yet. §6.5 says "model +
   *  provider IN USE", and the two genuinely differ — a model policy can route
   *  a turn somewhere other than the binding. */
  readonly actual: boolean;
}

/**
 * Prefer what a run actually used over what the agent is configured with.
 *
 * Takes the newest run that RECORDED a model, rather than simply the newest
 * run. `provider`/`model` have two writers: `insert_run`, which copies them from
 * a `RunCreate` that defaults BOTH to None, and `finalize_run`, which stamps
 * what the gateway really used. So an in-flight run almost always carries nulls,
 * and preferring it would make every working agent fall back to its configured
 * binding — labelling a busy agent "(configured)" while thirty finished runs sat
 * right behind it recording the real model.
 */
export function modelInUse(
  agent: AgentOut,
  runs: readonly RunOut[],
): ModelInUse {
  const run = newestFirst(runs).find(
    (r) => r.model != null || r.provider != null,
  );
  if (run) {
    return { provider: run.provider, model: run.model, actual: true };
  }
  const binding = agent.model_binding as {
    provider?: unknown;
    model?: unknown;
  };
  return {
    provider: typeof binding?.provider === "string" ? binding.provider : null,
    model: typeof binding?.model === "string" ? binding.model : null,
    actual: false,
  };
}

/** The task the agent is on right now — from its run, since the column that
 *  claims to hold this is never written. */
export function currentTaskId(runs: readonly RunOut[]): string | null {
  return activeRun(runs)?.task_id ?? null;
}

/**
 * The newest failure worth surfacing (its `error` text and a link target).
 *
 * Null while the agent has live work, so this cannot contradict
 * {@link agentState}. Both must obey the same rule — "a live run wins over a
 * stale failure" — or the card says two things at once: a "working" chip above
 * red error text, which is exactly what happens when an agent owns a long
 * running build (run A) and a newer short run (run B) failed beside it.
 */
export function lastFailure<T extends RunOut>(runs: readonly T[]): T | null {
  if (activeRun(runs) !== null) {
    return null;
  }
  const last = latestRun(runs);
  return last !== null && TERMINAL_FAILURES.includes(last.status) ? last : null;
}

export type Rollup = Record<AgentState, number>;

/** The header's "12 working · 2 idle · 1 error" counts. */
export function rollup(states: readonly AgentState[]): Rollup {
  const counts: Rollup = { working: 0, idle: 0, paused: 0, error: 0 };
  for (const state of states) {
    counts[state] += 1;
  }
  return counts;
}

/** Group an agent's runs by agent id, so one project-wide fetch serves every
 *  card rather than one request per agent. */
export function runsByAgent<T extends RunOut>(
  runs: readonly T[],
): Map<string, T[]> {
  const byAgent = new Map<string, T[]>();
  for (const run of runs) {
    const list = byAgent.get(run.agent_id);
    if (list) {
      list.push(run);
    } else {
      byAgent.set(run.agent_id, [run]);
    }
  }
  return byAgent;
}
