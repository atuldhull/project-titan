/**
 * Named extension slots + the panel trust boundary (TITAN-INT-011).
 *
 * `mount` and `src` are BOTH unconstrained plugin-authored strings — the
 * manifest schema gives them only a length limit — so these functions are the
 * only thing standing between a manifest and an arbitrary origin framed inside
 * the authenticated dashboard. Most of this file is adversarial.
 */
import { describe, expect, it } from "vitest";

import {
  isKnownSlot,
  KNOWN_SLOTS,
  PANEL_SANDBOX,
  panelUrl,
  panelsForSlot,
  unknownMounts,
  type PanelSource,
} from "@/lib/plugins/slots";

const install = (over: Partial<PanelSource> = {}): PanelSource => ({
  plugin_id: "titan.sample",
  state: "enabled",
  manifest: {
    name: "Sample Connector",
    contributes: {
      ui_panels: [
        {
          id: "sample-panel",
          title: "Sample",
          mount: "project.sidebar",
          type: "iframe",
          src: "/plugin-ui/titan.sample/panel.html",
        },
      ],
    },
  },
  ...over,
});

describe("panelUrl", () => {
  it("routes a valid panel through the same-origin BFF", () => {
    // /plugin-ui/... is a GUARDED route: an iframe sends no Authorization
    // header, so framing the API origin directly gets 401. The BFF re-attaches
    // the bearer from the httpOnly cookie.
    expect(panelUrl("titan.sample", "/plugin-ui/titan.sample/panel.html")).toBe(
      "/api/titan/plugin-ui/titan.sample/panel.html",
    );
  });

  it("refuses an absolute URL to another origin", () => {
    // The whole point: a manifest must not be able to frame evil.test inside
    // an authenticated dashboard.
    expect(panelUrl("titan.sample", "https://evil.test/panel.html")).toBeNull();
    expect(panelUrl("titan.sample", "http://evil.test/panel.html")).toBeNull();
  });

  it("refuses a protocol-relative URL", () => {
    // "//evil.test/x" starts with "/" and looks same-origin to a naive check,
    // but the browser resolves it to another ORIGIN entirely.
    expect(panelUrl("titan.sample", "//evil.test/panel.html")).toBeNull();
  });

  it("refuses a path belonging to a different plugin", () => {
    // titan.a must not serve titan.b's assets under its own name.
    expect(panelUrl("titan.a", "/plugin-ui/titan.b/panel.html")).toBeNull();
  });

  it("refuses traversal out of the plugin's asset directory", () => {
    expect(
      panelUrl("titan.sample", "/plugin-ui/titan.sample/../../etc/passwd"),
    ).toBeNull();
  });

  it.each([
    ["percent-encoded dots", "/plugin-ui/titan.sample/%2e%2e/%2e%2e/secret"],
    ["half-encoded dots", "/plugin-ui/titan.sample/.%2e/.%2e/secret"],
    ["uppercase encoding", "/plugin-ui/titan.sample/%2E%2E/secret"],
    ["backslash", "/plugin-ui/titan.sample/..\\..\\secret"],
    ["embedded tab", "/plugin-ui/titan.sample/.\t./../secret"],
    ["embedded newline", "/plugin-ui/titan.sample/.\n./../secret"],
  ])("refuses ENCODED traversal (%s)", (_name, src) => {
    // A literal ".." substring test passes every one of these, and the browser
    // then normalises them into a real escape. The check has to resolve the
    // URL, not pattern-match the string.
    const url = panelUrl("titan.sample", src);
    if (url !== null) {
      // If it is accepted at all, it must have been normalised to something
      // still inside this plugin's own directory.
      expect(url.startsWith("/api/titan/plugin-ui/titan.sample/")).toBe(true);
      expect(url).not.toContain("..");
    }
  });

  it("normalises a redundant segment rather than passing it through raw", () => {
    // "/a/./b" is legal and resolves inside the plugin; what must never happen
    // is the raw string reaching the proxy unnormalised.
    const url = panelUrl(
      "titan.sample",
      "/plugin-ui/titan.sample/./panel.html",
    );
    expect(url).toBe("/api/titan/plugin-ui/titan.sample/panel.html");
  });

  it("refuses a path that resolves to the directory itself", () => {
    expect(panelUrl("titan.sample", "/plugin-ui/titan.sample/sub/")).toBeNull();
  });

  it("refuses a query string or fragment", () => {
    // The asset route resolves a literal path; anything else is a smuggling
    // attempt rather than a meaningful request.
    expect(
      panelUrl("titan.sample", "/plugin-ui/titan.sample/p.html?x=1"),
    ).toBeNull();
    expect(
      panelUrl("titan.sample", "/plugin-ui/titan.sample/p.html#x"),
    ).toBeNull();
  });

  it("refuses an empty asset path", () => {
    expect(panelUrl("titan.sample", "/plugin-ui/titan.sample/")).toBeNull();
  });

  it("is null for a panel with no src at all", () => {
    expect(panelUrl("titan.sample", undefined)).toBeNull();
  });

  it("refuses a relative path", () => {
    expect(panelUrl("titan.sample", "panel.html")).toBeNull();
  });
});

describe("panelsForSlot", () => {
  it("mounts a declared panel at its slot", () => {
    const panels = panelsForSlot([install()], "project.sidebar");
    expect(panels).toHaveLength(1);
    expect(panels[0].title).toBe("Sample");
    expect(panels[0].pluginName).toBe("Sample Connector");
    expect(panels[0].url).toBe("/api/titan/plugin-ui/titan.sample/panel.html");
  });

  it("ignores a plugin that is installed but not enabled", () => {
    // Disable clears granted_permissions, so the panel's every backing call
    // would be denied — rendering it would show a broken UI, not a disabled one.
    expect(
      panelsForSlot([install({ state: "installed" })], "project.sidebar"),
    ).toEqual([]);
    expect(
      panelsForSlot([install({ state: "quarantined" })], "project.sidebar"),
    ).toEqual([]);
  });

  it("DROPS a panel declaring a slot core does not know", () => {
    // `mount` is free-form, so without an allowlist a plugin could name any
    // string and a permissive renderer might place it somewhere arbitrary.
    const rogue = install({
      manifest: {
        name: "Rogue",
        contributes: {
          ui_panels: [
            {
              id: "x",
              title: "X",
              mount: "topbar.admin",
              src: "/plugin-ui/titan.sample/x.html",
            },
          ],
        },
      },
    });
    for (const slot of KNOWN_SLOTS) {
      expect(panelsForSlot([rogue], slot)).toEqual([]);
    }
  });

  it("still mounts a panel whose src is invalid, with a null url", () => {
    // Dropping it entirely would hide a misconfiguration; the component renders
    // an explanation instead of a silent gap.
    const bad = install({
      manifest: {
        name: "Bad",
        contributes: {
          ui_panels: [
            {
              id: "x",
              title: "X",
              mount: "project.sidebar",
              src: "https://evil.test/x",
            },
          ],
        },
      },
    });
    const panels = panelsForSlot([bad], "project.sidebar");
    expect(panels).toHaveLength(1);
    expect(panels[0].url).toBeNull();
  });

  it("defaults an unknown panel type to iframe", () => {
    const odd = install({
      manifest: {
        name: "Odd",
        contributes: {
          ui_panels: [
            {
              id: "x",
              title: "X",
              mount: "project.sidebar",
              type: "webgl",
              src: "/plugin-ui/titan.sample/x.html",
            },
          ],
        },
      },
    });
    expect(panelsForSlot([odd], "project.sidebar")[0].type).toBe("iframe");
  });

  it("orders panels deterministically across plugins", () => {
    // Two plugins in one slot must not swap places between renders because the
    // install list came back in a different order.
    const b = install({ plugin_id: "titan.b" });
    const a = install({ plugin_id: "titan.a" });
    expect(
      panelsForSlot([b, a], "project.sidebar").map((p) => p.pluginId),
    ).toEqual(["titan.a", "titan.b"]);
  });

  it("survives a manifest with no contributes block", () => {
    expect(
      panelsForSlot(
        [install({ manifest: { name: "Bare" } })],
        "project.sidebar",
      ),
    ).toEqual([]);
    expect(
      panelsForSlot([install({ manifest: null })], "project.sidebar"),
    ).toEqual([]);
  });

  it("is empty for no installs", () => {
    expect(panelsForSlot([], "project.sidebar")).toEqual([]);
  });
});

describe("unknownMounts", () => {
  it("reports a mount core cannot render, so it is not silently invisible", () => {
    const rogue = install({
      manifest: {
        contributes: {
          ui_panels: [{ id: "x", title: "X", mount: "nowhere.real" }],
        },
      },
    });
    expect(unknownMounts([rogue])).toEqual([
      { pluginId: "titan.sample", mount: "nowhere.real" },
    ]);
  });

  it("says nothing about a known mount", () => {
    expect(unknownMounts([install()])).toEqual([]);
  });
});

describe("the sandbox", () => {
  it("grants scripts but NEVER same-origin", () => {
    // The pairing is the whole containment: allow-scripts WITH allow-same-origin
    // gives the frame this document's origin — its cookies, its localStorage,
    // and the session. There is no server CSP behind this (the BFF's response
    // header allowlist does not forward one), so this attribute is the boundary.
    expect(PANEL_SANDBOX).toContain("allow-scripts");
    expect(PANEL_SANDBOX).not.toContain("allow-same-origin");
    expect(PANEL_SANDBOX).not.toContain("allow-top-navigation");
  });
});

describe("isKnownSlot", () => {
  it("accepts the slots core renders and nothing else", () => {
    expect(isKnownSlot("project.sidebar")).toBe(true);
    expect(isKnownSlot("project.overview")).toBe(true);
    expect(isKnownSlot("topbar.admin")).toBe(false);
    expect(isKnownSlot("")).toBe(false);
  });
});
