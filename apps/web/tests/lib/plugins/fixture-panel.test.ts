/**
 * DoD: "a sample fixture plugin's panel renders in a named slot with no
 * core-code changes."
 *
 * The other slot tests use synthetic manifests, which proves the resolver's
 * logic but not that the REAL fixture satisfies it. This drives the actual
 * `plugins/first_party/sample/titan-plugin.yaml` off disk, so it fails if
 * anyone edits the fixture's mount, src or id out of alignment with the slot
 * registry — the failure mode where every unit test stays green and the panel
 * silently stops appearing.
 *
 * `apps/web` has no YAML parser and this does not justify adding a dependency,
 * so the handful of scalar fields are extracted directly. Every extraction is
 * REQUIRED to match: a manifest whose shape changed enough to defeat these
 * patterns throws, rather than yielding an empty panel list that would let the
 * test pass while proving nothing.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  isKnownSlot,
  panelsForSlot,
  type PanelSource,
} from "@/lib/plugins/slots";

const MANIFEST_PATH = join(
  process.cwd(),
  "..",
  "..",
  "plugins",
  "first_party",
  "sample",
  "titan-plugin.yaml",
);

const SOURCE = readFileSync(MANIFEST_PATH, "utf8");

/** A required top-level scalar. Throws rather than returning undefined. */
function required(field: string): string {
  const match = new RegExp(`^${field}:\\s*(.+)$`, "m").exec(SOURCE);
  if (!match) {
    throw new Error(
      `${MANIFEST_PATH} no longer has a top-level '${field}:' — this test ` +
        `cannot verify the fixture and must be updated, not deleted.`,
    );
  }
  return match[1].trim();
}

/** The single ui_panels entry, as the API would hand it back. */
function panelEntry(): Record<string, string> {
  const block = /ui_panels:\n([\s\S]*?)(?:\n {2}\w|\n\w|$)/.exec(SOURCE);
  if (!block) {
    throw new Error(
      `${MANIFEST_PATH} declares no ui_panels — DoD #1 is unmet.`,
    );
  }
  const entry: Record<string, string> = {};
  for (const field of ["id", "title", "mount", "type", "src"]) {
    const match = new RegExp(`^\\s*-?\\s*${field}:\\s*(.+)$`, "m").exec(
      block[1],
    );
    if (!match) {
      throw new Error(`the sample plugin's ui_panel has no '${field}'`);
    }
    entry[field] = match[1].trim();
  }
  return entry;
}

/** The install row the API would return for the enabled fixture plugin. */
function installedSample(state = "enabled"): PanelSource {
  return {
    plugin_id: required("id"),
    state,
    manifest: {
      name: required("name"),
      contributes: { ui_panels: [panelEntry()] },
    },
  };
}

describe("the sample plugin's panel (DoD)", () => {
  it("declares a mount the slot registry knows", () => {
    // `mount` is an unconstrained free-form string in the manifest schema, so a
    // typo would be accepted by the backend and then render nowhere at all.
    expect(isKnownSlot(panelEntry().mount)).toBe(true);
  });

  it("resolves into project.sidebar with a loadable same-origin url", () => {
    const mounted = panelsForSlot([installedSample()], "project.sidebar");
    expect(mounted).toHaveLength(1);
    const panel = mounted[0];
    expect(panel.pluginId).toBe("titan.sample");
    expect(panel.pluginName).toBe("Sample Connector");
    expect(panel.type).toBe("iframe");
    // A null url renders the "will not load" fallback instead of the panel —
    // i.e. the DoD would read as met while showing nothing.
    expect(panel.url).toBe("/api/titan/plugin-ui/titan.sample/panel.html");
  });

  it("disappears when the plugin is not enabled", () => {
    expect(
      panelsForSlot([installedSample("installed")], "project.sidebar"),
    ).toEqual([]);
  });

  it("declares a config schema, so the generated form has something to render", () => {
    // TITAN-FRT-035's fixture requirement. `config_schema` is a PATH; the
    // backend test asserts that the file resolves and parses.
    expect(required("config_schema")).toBe("config.schema.json");
  });
});
