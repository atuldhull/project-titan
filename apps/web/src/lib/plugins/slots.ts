/**
 * Named dashboard extension slots (TITAN-INT-011, Vol 4 §6.12).
 *
 * A plugin contributes UI by declaring `contributes.ui_panels[].mount` in its
 * manifest; core renders whatever is mounted at a slot it knows about, with no
 * change to core packages. That is the whole requirement — and the interesting
 * part is what it means for trust.
 *
 * **`mount` is an unconstrained plugin-authored string.** The manifest schema
 * gives it only a length limit: no enum, no validator, no known-slot list
 * anywhere in the backend (`project.sidebar` appears once, in a Python
 * comment). So the allowlist has to live here, and unknown mounts are DROPPED
 * rather than rendered somewhere arbitrary — a plugin must not be able to
 * choose where in the shell it appears by inventing a slot name.
 *
 * **`src` is plugin-authored too**, and it names a URL. {@link panelUrl} is the
 * only thing standing between a manifest and an arbitrary third-party origin
 * being framed inside the dashboard, so it accepts exactly one shape: a
 * same-origin `/plugin-ui/<id>/…` path belonging to the declaring plugin.
 */

/** The slots core actually renders. A mount not in this set is not a slot. */
export const KNOWN_SLOTS = [
  "project.sidebar",
  "project.overview",
  "settings.panel",
] as const;

export type SlotName = (typeof KNOWN_SLOTS)[number];

export function isKnownSlot(mount: string): mount is SlotName {
  return (KNOWN_SLOTS as readonly string[]).includes(mount);
}

/** The panel types the manifest schema permits. */
export type PanelType = "iframe" | "schema-form" | "markdown";

export interface UiPanel {
  readonly id: string;
  readonly title: string;
  readonly mount: string;
  readonly type?: string;
  readonly src?: string;
}

export interface MountedPanel {
  readonly pluginId: string;
  readonly pluginName: string;
  readonly id: string;
  readonly title: string;
  readonly slot: SlotName;
  readonly type: PanelType;
  /** Same-origin BFF URL, or null when `src` did not validate. */
  readonly url: string | null;
}

/** An installed plugin, as far as slot resolution needs it. */
export interface PanelSource {
  readonly plugin_id: string;
  readonly state: string;
  readonly manifest?: Record<string, unknown> | null;
}

const PANEL_TYPES: readonly string[] = ["iframe", "schema-form", "markdown"];

function panelsOf(manifest: Record<string, unknown> | null | undefined) {
  const contributes = manifest?.contributes;
  if (typeof contributes !== "object" || contributes === null) return [];
  const panels = (contributes as Record<string, unknown>).ui_panels;
  return Array.isArray(panels) ? panels : [];
}

function nameOf(manifest: Record<string, unknown> | null | undefined): string {
  const name = manifest?.name;
  return typeof name === "string" && name ? name : "";
}

/**
 * The same-origin URL for a panel's `src`, or null if it is not one.
 *
 * Three separate reasons this cannot just pass `src` through:
 *
 * 1. **The API origin needs a bearer token.** `/plugin-ui/...` is a guarded
 *    route, and an `<iframe>` sends no Authorization header — a direct frame of
 *    the API origin gets 401. Every browser call in this app goes through the
 *    same-origin BFF at `/api/titan/...`, which re-attaches the token, so the
 *    panel must too.
 * 2. **`src` is plugin-authored.** Passing it through unchecked would let a
 *    manifest frame `https://evil.test` inside the authenticated dashboard.
 * 3. **A plugin must only serve its own assets.** The path is required to name
 *    the declaring plugin, so `titan.a` cannot point at `/plugin-ui/titan.b/…`.
 */
/** Throwaway base for resolving a panel path. Never requested; its only job is
 *  to give `new URL` an origin to normalise against and compare back to. */
const BASE = "https://panel.invalid";

export function panelUrl(
  pluginId: string,
  src: string | undefined,
): string | null {
  if (!src) return null;
  // Reject a scheme or protocol-relative prefix first: "//evil.test/x" starts
  // with "/" and looks same-origin to a naive check, but the browser resolves
  // it to a different ORIGIN.
  if (!src.startsWith("/") || src.startsWith("//")) return null;

  // RESOLVE, don't pattern-match. A literal ".." substring test is defeated by
  // every encoding the URL parser normalises away for us — "%2e%2e", ".%2e",
  // backslashes, and embedded tab/newline (which browsers strip BEFORE
  // parsing). Checking the NORMALISED pathname is the only version of this
  // that sees what the browser will actually request.
  let parsed: URL;
  try {
    parsed = new URL(src, BASE);
  } catch {
    return null;
  }
  if (parsed.origin !== BASE) return null;
  // No query, no fragment: the asset route resolves a literal path under the
  // plugin's ui/ directory and nothing else is meaningful there.
  if (parsed.search !== "" || parsed.hash !== "") return null;

  const expected = `/plugin-ui/${pluginId}/`;
  if (!parsed.pathname.startsWith(expected)) return null;
  const rest = parsed.pathname.slice(expected.length);
  if (!rest || rest.endsWith("/")) return null;
  // Rebuild from the NORMALISED pathname, never the raw input.
  src = parsed.pathname;
  return `/api/titan${src}`;
}

/**
 * Every panel mounted at `slot`, from the plugins that are ENABLED.
 *
 * Enabled-only is deliberate: an installed-but-disabled plugin has had its
 * granted permissions cleared, so rendering its UI would show a panel whose
 * every backing call is denied.
 */
export function panelsForSlot(
  installs: readonly PanelSource[],
  slot: SlotName,
): readonly MountedPanel[] {
  const mounted: MountedPanel[] = [];
  for (const install of installs) {
    if (install.state !== "enabled") continue;
    for (const raw of panelsOf(install.manifest)) {
      if (typeof raw !== "object" || raw === null) continue;
      const panel = raw as unknown as UiPanel;
      if (typeof panel.mount !== "string" || panel.mount !== slot) continue;
      if (typeof panel.id !== "string" || typeof panel.title !== "string") {
        continue;
      }
      const type: PanelType = PANEL_TYPES.includes(panel.type ?? "")
        ? (panel.type as PanelType)
        : "iframe";
      mounted.push({
        pluginId: install.plugin_id,
        pluginName: nameOf(install.manifest) || install.plugin_id,
        id: panel.id,
        title: panel.title,
        slot,
        type,
        url: panelUrl(install.plugin_id, panel.src),
      });
    }
  }
  // Stable order: two plugins mounting the same slot must not swap places
  // between renders just because the install list came back in a different
  // order.
  return mounted.sort(
    (a, b) => a.pluginId.localeCompare(b.pluginId) || a.id.localeCompare(b.id),
  );
}

/** Panels declaring a mount core does not know about, for the Plugin Manager. */
export function unknownMounts(
  installs: readonly PanelSource[],
): readonly { pluginId: string; mount: string }[] {
  const out: { pluginId: string; mount: string }[] = [];
  for (const install of installs) {
    for (const raw of panelsOf(install.manifest)) {
      const panel = raw as UiPanel;
      if (typeof panel?.mount === "string" && !isKnownSlot(panel.mount)) {
        out.push({ pluginId: install.plugin_id, mount: panel.mount });
      }
    }
  }
  return out;
}

/**
 * The `sandbox` attribute for a panel iframe.
 *
 * `allow-scripts` without `allow-same-origin` is the important pairing: the
 * frame gets a unique opaque origin, so its script cannot reach this document,
 * its cookies, or its localStorage — where the session lives. Granting both
 * together would be equivalent to removing the sandbox entirely.
 *
 * There is no server-side CSP to lean on: the BFF proxy forwards a fixed
 * seven-header allowlist that does not include `content-security-policy`, so
 * the sandbox attribute is the containment, not a second line of defence.
 */
export const PANEL_SANDBOX = "allow-scripts allow-forms allow-popups";
