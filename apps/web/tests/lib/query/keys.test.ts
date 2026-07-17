/**
 * The key factory's collision and prefix rules (TITAN-FRT-017).
 *
 * Keys are PREFIXES in React Query: invalidating `["project", pid]` matches
 * `["project", pid, "board"]`. That is load-bearing (it is what makes the task
 * Sheet live), so it is asserted here against a real QueryClient rather than
 * reasoned about — twice now a review has claimed a missing invalidation that
 * prefix-matching already covered, and only an actual client settled it.
 *
 * The same rule has a sharp edge: two queries sharing a key share a cache
 * entry. `agents()` and the agent roster used to do exactly that with
 * incompatible shapes.
 */

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { queryKeys } from "@/lib/query/keys";

const PID = "11111111-1111-7111-8111-111111111111";

describe("queryKeys: distinct endpoints never share a cache entry", () => {
  it("separates the runtime agent list from the declarative roster", () => {
    // The bug this pins: both were `["agents"]`. `useAgents()` resolves
    // GET /api/v1/agents -> Page<AgentOut> (an OBJECT with .items);
    // `useAgentDefinitions()` resolves GET /api/v1/agent-definitions ->
    // AgentDefinition[] (an ARRAY). With staleTime 30s, visiting /org (which
    // fills the key with a Page) and then /agents inside the window handed the
    // roster the Page and did not refetch, and the roster does `agents.filter`
    // -> "agents.filter is not a function".
    const client = new QueryClient();
    client.setQueryData(queryKeys.agents(), { items: [], next_cursor: null });
    client.setQueryData(queryKeys.agentDefinitions(), [{ agent_id: "ceo" }]);

    expect(client.getQueryData(queryKeys.agentDefinitions())).toEqual([
      { agent_id: "ceo" },
    ]);
    expect(client.getQueryData(queryKeys.agents())).toEqual({
      items: [],
      next_cursor: null,
    });
  });

  it("does not let one agent's key collide with the list", () => {
    // `agent(id)` is ["agents", id] — a CHILD of ["agents"]. That is deliberate
    // (an agent.status frame invalidating the list should refresh an open
    // detail too), but the two must still hold their own data.
    //
    // Asserting only that `agent("abc")` reads back would pass under the very
    // collision it names: writing the two keys in order and reading the LAST one
    // returns it whether or not they are the same entry. So assert the keys are
    // DISTINCT and that BOTH survive — the writes are ordered list-then-detail,
    // so a collision destroys the list.
    const client = new QueryClient();
    expect(queryKeys.agent("abc")).not.toEqual(queryKeys.agents());
    client.setQueryData(queryKeys.agents(), { items: [] });
    client.setQueryData(queryKeys.agent("abc"), { id: "abc" });
    expect(client.getQueryData(queryKeys.agent("abc"))).toEqual({ id: "abc" });
    expect(client.getQueryData(queryKeys.agents())).toEqual({ items: [] });
    expect(client.getQueryCache().getAll()).toHaveLength(2);
  });
});

describe("queryKeys: the prefix relationships the WS layer relies on", () => {
  it("invalidating a project reaches its runs, board and messages", () => {
    const client = new QueryClient();
    for (const key of [
      queryKeys.projectRuns(PID),
      queryKeys.projectBoard(PID),
      queryKeys.projectMessages(PID),
      queryKeys.projectAgentRuns(PID, "agent-1"),
    ]) {
      client.setQueryData(key, "cached");
    }
    const matched = client
      .getQueryCache()
      .findAll({ queryKey: queryKeys.project(PID) });
    expect(matched).toHaveLength(4);
  });

  it("scopes an agent's runs under the project's runs, not the agent list", () => {
    // A run frame invalidates projectRuns; the drawer's per-agent run query
    // must ride that invalidation, so it has to be a CHILD of projectRuns.
    const client = new QueryClient();
    client.setQueryData(queryKeys.projectAgentRuns(PID, "agent-1"), "cached");
    expect(
      client.getQueryCache().findAll({ queryKey: queryKeys.projectRuns(PID) }),
    ).toHaveLength(1);
    expect(
      client.getQueryCache().findAll({ queryKey: queryKeys.agents() }),
    ).toHaveLength(0);
  });

  it("keeps one agent's messages under the project's messages", () => {
    const client = new QueryClient();
    client.setQueryData(queryKeys.agentMessages(PID, "agent-1"), "cached");
    expect(
      client
        .getQueryCache()
        .findAll({ queryKey: queryKeys.projectMessages(PID) }),
    ).toHaveLength(1);
  });
});
