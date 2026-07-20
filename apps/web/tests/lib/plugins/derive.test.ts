/**
 * Plugin Manager view model (Vol 4 §6.12).
 *
 * The interesting cases are the ones where the product's reality differs from
 * what §6.12's wireframe implies: there is no health column, no
 * `plugin.status_changed`, and no per-capability approval.
 */
import { describe, expect, it } from "vitest";

import type { PluginInstallOut, PluginOut } from "@titan/api-client";

import {
  availableFrom,
  enableBlockers,
  grantsNothing,
  healthOf,
  matchesQuery,
  requestedPermissions,
  toRow,
} from "@/lib/plugins/derive";

const install = (over: Partial<PluginInstallOut> = {}): PluginInstallOut =>
  ({
    plugin_id: "titan.sample",
    installed_version: "1.0.0",
    state: "enabled",
    content_hash: "abc",
    granted_permissions: {},
    manifest: { name: "Sample", config_schema: "config.schema.json" },
    error_count: 0,
    last_error: "",
    tools: ["sample.echo"],
    ...over,
  }) as PluginInstallOut;

describe("healthOf", () => {
  it("is healthy only when enabled AND error-free", () => {
    expect(healthOf(install()).level).toBe("healthy");
  });

  it("reports an enabled plugin with errors as an error, not healthy", () => {
    // A flat "healthy" here would hide exactly the state §6.12's [Reconnect]
    // affordance exists for.
    const unhappy = healthOf(install({ error_count: 3 }));
    expect(unhappy.level).toBe("error");
    expect(unhappy.label).toContain("3");
  });

  it("surfaces the quarantine reason", () => {
    // Quarantine is what makes a failed healthcheck OBSERVABLE at all: there is
    // no health column, so the supervisor moving the row is the signal.
    const out = healthOf(
      install({ state: "quarantined", last_error: "auth expired" }),
    );
    expect(out.level).toBe("error");
    expect(out.label).toBe("auth expired");
  });

  it("treats installed-but-not-enabled as idle, not broken", () => {
    expect(healthOf(install({ state: "installed" })).level).toBe("idle");
  });
});

describe("toRow", () => {
  it("reads the display name from the manifest, else the id", () => {
    expect(toRow(install()).name).toBe("Sample");
    expect(toRow(install({ manifest: {} })).name).toBe("titan.sample");
  });

  it("knows whether a config form is available at all", () => {
    // `config_schema` is a PATH string on the manifest; no path means the gear
    // icon would open an empty dialog.
    expect(toRow(install()).hasConfig).toBe(true);
    expect(toRow(install({ manifest: { name: "x" } })).hasConfig).toBe(false);
  });

  it("detects an MCP runtime", () => {
    expect(
      toRow(install({ manifest: { runtime: { type: "mcp" } } })).isMcp,
    ).toBe(true);
    expect(
      toRow(install({ manifest: { runtime: { type: "python" } } })).isMcp,
    ).toBe(false);
  });

  it("survives a manifest that is missing or malformed", () => {
    expect(() => toRow(install({ manifest: undefined }))).not.toThrow();
    expect(toRow(install({ manifest: undefined })).panels).toBe(0);
  });
});

describe("availableFrom", () => {
  const catalogue = [
    { plugin_id: "titan.a", name: "A" },
    { plugin_id: "titan.b", name: "B" },
  ] as PluginOut[];

  it("hides what is already installed", () => {
    const out = availableFrom(catalogue, [install({ plugin_id: "titan.a" })]);
    expect(out.map((p) => p.plugin_id)).toEqual(["titan.b"]);
  });

  it("re-offers a plugin that was REMOVED", () => {
    // `removed` is a lifecycle state, not a deletion — the row stays. Treating
    // it as installed would make an uninstalled plugin unreinstallable.
    const out = availableFrom(catalogue, [
      install({ plugin_id: "titan.a", state: "removed" }),
    ]);
    expect(out.map((p) => p.plugin_id)).toEqual(["titan.a", "titan.b"]);
  });

  it("sorts by name so the catalog does not reshuffle between renders", () => {
    const shuffled = [...catalogue].reverse();
    expect(availableFrom(shuffled, []).map((p) => p.name)).toEqual(["A", "B"]);
  });
});

describe("requestedPermissions", () => {
  const withPermissions = install({
    manifest: {
      permissions: {
        scopes: ["task:read"],
        network: ["https://api.example.com"],
        secrets: ["SAMPLE_TOKEN"],
        events: {
          publish: ["integration.synced"],
          subscribe: ["task.created"],
        },
      },
    },
  });

  it("lists every declared class, because enable grants them all at once", () => {
    // There is no per-capability approval anywhere in the runtime, so this list
    // IS the consent — summarising it would be consenting on the operator's
    // behalf.
    const kinds = requestedPermissions(withPermissions).map((p) => p.kind);
    expect(kinds).toContain("RBAC scopes");
    expect(kinds).toContain("Network");
    expect(kinds).toContain("Secrets");
    expect(kinds).toContain("Publishes events");
  });

  it("says that event subscriptions are not actually delivered", () => {
    // Declarable, counted as a widening on upgrade, and never delivered — there
    // is no core→plugin event fan-in. Listing it without the caveat would
    // describe a capability that does not function.
    const subscribe = requestedPermissions(withPermissions).find(
      (p) => p.kind === "Subscribes to events",
    );
    expect(subscribe?.note).toContain("Not delivered");
  });

  it("omits a class the manifest does not declare", () => {
    const kinds = requestedPermissions(
      install({ manifest: { permissions: { scopes: ["task:read"] } } }),
    ).map((p) => p.kind);
    expect(kinds).toEqual(["RBAC scopes"]);
  });

  it("reports a plugin that asks for nothing", () => {
    expect(grantsNothing(install({ manifest: {} }))).toBe(true);
    expect(grantsNothing(withPermissions)).toBe(false);
  });
});

describe("enableBlockers", () => {
  it("names the env var for each unresolved secret", () => {
    // Enable is all-or-nothing and a missing secret surfaces as a failed start,
    // so saying so up front beats a generic failure after the fact.
    const blockers = enableBlockers([
      {
        name: "SAMPLE_TOKEN",
        resolved: false,
        env_var: "TITAN_PLUGIN_SECRET_SAMPLE_TOKEN",
      },
      { name: "OTHER", resolved: true, env_var: "TITAN_PLUGIN_SECRET_OTHER" },
    ]);
    expect(blockers).toHaveLength(1);
    expect(blockers[0]).toContain("TITAN_PLUGIN_SECRET_SAMPLE_TOKEN");
  });

  it("is empty when every secret resolves", () => {
    expect(
      enableBlockers([{ name: "A", resolved: true, env_var: "X" }]),
    ).toEqual([]);
  });
});

describe("matchesQuery", () => {
  const slack = {
    id: "titan.slack",
    name: "Slack Connector",
    description: "Posts task events into a channel.",
  };

  it("matches everything for an empty or whitespace-only query", () => {
    // The bug this guards is the one a user actually hits: clearing the filter
    // box must restore the list, not empty it. `"   "` is the same state as
    // `""` — the box looks cleared and must behave cleared.
    expect(matchesQuery(slack, "")).toBe(true);
    expect(matchesQuery(slack, "   ")).toBe(true);
  });

  it("matches on the id, the name and the description", () => {
    // Three fields because a user types whichever one they last saw: the id
    // from a manifest, the display name from this screen, or a word from the
    // description.
    expect(matchesQuery(slack, "titan.sl")).toBe(true);
    expect(matchesQuery(slack, "Connector")).toBe(true);
    expect(matchesQuery(slack, "channel")).toBe(true);
  });

  it("is case-insensitive in both directions", () => {
    // The id is deliberately NOT `titan.slack` here. With it, both assertions
    // pass against an implementation that lowercases only the QUERY — the
    // already-lowercase id matches and masks the missing `field.toLowerCase()`.
    // Verified by mutation: dropping it must fail this test, and with the
    // realistic id it did not.
    const mixed = { id: "acme.connector", name: "Slack", description: "" };
    expect(matchesQuery(mixed, "SLACK")).toBe(true); // upper query, mixed field
    expect(matchesQuery({ ...mixed, name: "SLACK" }, "slack")).toBe(true); // and the reverse
  });

  it("trims the query before matching", () => {
    // A trailing space is what a paste or an autocorrect leaves behind, and
    // matching it literally would find nothing for no visible reason.
    expect(matchesQuery(slack, "  slack  ")).toBe(true);
  });

  it("rejects a non-match", () => {
    expect(matchesQuery(slack, "github")).toBe(false);
  });

  it("tolerates a missing description", () => {
    // `PluginOut.description` is optional in the generated types; a catalogue
    // entry without one must still be filterable rather than throwing.
    expect(matchesQuery({ id: "a.b", name: "Thing" }, "thing")).toBe(true);
    expect(matchesQuery({ id: "a.b", name: "Thing" }, "zzz")).toBe(false);
  });
});
