/**
 * Plugin Manager view model (Vol 4 §6.12).
 *
 * Pure functions over what the API really returns, kept out of the components
 * so the parts that can be wrong — lifecycle state, health, the permission
 * consent list — are testable without a DOM.
 */
import type {
  PluginInstallOut,
  PluginOut,
  PluginSecretStatus,
} from "@titan/api-client";

/** The lifecycle states `plugin_installs.state` is CHECK-constrained to. */
export type PluginState =
  "installed" | "enabled" | "quarantined" | "removed" | "available";

/**
 * Health as §6.12 draws it (✔ healthy / ✖ error / ◐ …).
 *
 * There is no health column and no `plugin.status_changed` event — zero
 * producers anywhere in the repo — so this is derived from lifecycle state plus
 * the supervisor's live view, and the chip refreshes by polling. `quarantined`
 * is the real "✖ auth expired" case in the wireframe: a plugin that failed its
 * healthcheck is quarantined by the supervisor, which is what makes the error
 * state observable at all.
 */
export type HealthLevel = "healthy" | "error" | "idle" | "unknown";

export interface PluginRow {
  readonly pluginId: string;
  readonly name: string;
  readonly version: string;
  readonly description: string;
  readonly state: PluginState;
  readonly health: HealthLevel;
  readonly healthLabel: string;
  readonly lastError: string;
  readonly errorCount: number;
  readonly tools: readonly string[];
  readonly hasConfig: boolean;
  readonly isMcp: boolean;
  readonly panels: number;
}

function manifestOf(
  install: PluginInstallOut | PluginOut,
): Record<string, unknown> {
  const manifest = (install as { manifest?: unknown }).manifest;
  return typeof manifest === "object" && manifest !== null
    ? (manifest as Record<string, unknown>)
    : {};
}

function stringField(manifest: Record<string, unknown>, key: string): string {
  const value = manifest[key];
  return typeof value === "string" ? value : "";
}

export function healthOf(install: PluginInstallOut): {
  level: HealthLevel;
  label: string;
} {
  switch (install.state) {
    case "enabled":
      // An enabled plugin that has recorded errors is running but unhappy —
      // reporting a flat "healthy" would hide exactly the state §6.12's
      // "[Reconnect]" affordance exists for.
      return install.error_count > 0
        ? { level: "error", label: `${install.error_count} recent errors` }
        : { level: "healthy", label: "healthy" };
    case "quarantined":
      return {
        level: "error",
        label: install.last_error || "quarantined after a failed healthcheck",
      };
    case "installed":
      return { level: "idle", label: "installed, not enabled" };
    case "removed":
      return { level: "idle", label: "removed" };
    default:
      return { level: "unknown", label: install.state };
  }
}

export function toRow(install: PluginInstallOut): PluginRow {
  const manifest = manifestOf(install);
  const runtime = manifest.runtime;
  const contributes = manifest.contributes;
  const panels =
    typeof contributes === "object" && contributes !== null
      ? (contributes as Record<string, unknown>).ui_panels
      : undefined;
  const health = healthOf(install);
  return {
    pluginId: install.plugin_id,
    name: stringField(manifest, "name") || install.plugin_id,
    version: install.installed_version,
    description: stringField(manifest, "description"),
    state: install.state as PluginState,
    health: health.level,
    healthLabel: health.label,
    lastError: install.last_error ?? "",
    errorCount: install.error_count ?? 0,
    tools: install.tools ?? [],
    hasConfig: Boolean(stringField(manifest, "config_schema")),
    isMcp:
      typeof runtime === "object" &&
      runtime !== null &&
      (runtime as Record<string, unknown>).type === "mcp",
    panels: Array.isArray(panels) ? panels.length : 0,
  };
}

/**
 * Catalogue entries not yet installed.
 *
 * §6.12's Catalog tab. "Catalogue" is the local plugins directory, not a
 * marketplace: there is no registry fetch anywhere in the product, so the
 * spec's "catalog fetch failure falls back to the bundled offline catalog" is
 * vacuously satisfied — the bundled catalogue is the ONLY catalogue.
 */
export function availableFrom(
  catalogue: readonly PluginOut[],
  installs: readonly PluginInstallOut[],
): readonly PluginOut[] {
  const installed = new Set(
    installs.filter((i) => i.state !== "removed").map((i) => i.plugin_id),
  );
  return catalogue
    .filter((p) => !installed.has(p.plugin_id))
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Does this plugin match the screen's filter box (Vol 4 §10's `/` target)?
 *
 * Pure and here rather than inline in the component so the two things that are
 * easy to get wrong are tested: an empty or whitespace-only query matches
 * EVERYTHING (a filter that hides the whole list when you clear it is a bug that
 * reads as "the catalogue is empty"), and the match is case-insensitive across
 * all three fields a user might type — the display name, the id they saw in a
 * manifest, and the description.
 *
 * Takes the fields rather than a `PluginRow`, because the Catalog tab holds
 * `PluginOut` and the Installed tab holds `PluginRow`, and they name the id
 * differently. One predicate over both beats two that can disagree.
 */
export function matchesQuery(
  fields: {
    readonly id: string;
    readonly name: string;
    readonly description?: string;
  },
  query: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return [fields.id, fields.name, fields.description ?? ""].some((field) =>
    field.toLowerCase().includes(needle),
  );
}

export interface RequestedPermission {
  readonly kind: string;
  readonly values: readonly string[];
  readonly note?: string;
}

/**
 * What an install dialog must list, from `permissions` in the manifest.
 *
 * Vol 7 §3.4's five deny-by-default classes. Enable grants the whole declared
 * set at once — there is no per-capability approval — so this list IS the
 * consent, and it must be complete rather than summarised.
 */
export function requestedPermissions(
  source: PluginOut | PluginInstallOut,
): readonly RequestedPermission[] {
  const permissions = manifestOf(source).permissions;
  if (typeof permissions !== "object" || permissions === null) return [];
  const record = permissions as Record<string, unknown>;
  const out: RequestedPermission[] = [];

  const push = (kind: string, value: unknown, note?: string) => {
    const values = Array.isArray(value)
      ? value.filter((v): v is string => typeof v === "string")
      : [];
    if (values.length > 0) out.push({ kind, values, note });
  };

  push("RBAC scopes", record.scopes);
  push("Network", record.network);
  push("Filesystem", record.filesystem);
  push(
    "Secrets",
    record.secrets,
    "Resolved from the host environment — TITAN never stores their values.",
  );

  const events = record.events;
  if (typeof events === "object" && events !== null) {
    const eventRecord = events as Record<string, unknown>;
    push("Publishes events", eventRecord.publish);
    // Declarable, counted as a widening on upgrade — and never delivered:
    // there is no core→plugin event fan-in at all. Saying so in the consent
    // dialog is better than listing a capability that does not function.
    push(
      "Subscribes to events",
      eventRecord.subscribe,
      "Not delivered yet — no event fan-in to plugins exists.",
    );
  }
  return out;
}

/** True when enabling grants nothing at all — worth saying explicitly. */
export function grantsNothing(source: PluginOut | PluginInstallOut): boolean {
  return requestedPermissions(source).length === 0;
}

/** Secrets the plugin needs that the host cannot currently resolve. */
export function missingSecrets(
  secrets: readonly PluginSecretStatus[],
): readonly PluginSecretStatus[] {
  return secrets.filter((s) => !s.resolved);
}

/**
 * Whether `enable` can be expected to succeed.
 *
 * Enable is all-or-nothing and a missing secret surfaces as a failed start, so
 * saying "3 secrets are not set" up front beats a generic 500 after the fact.
 */
export function enableBlockers(
  secrets: readonly PluginSecretStatus[],
): readonly string[] {
  return missingSecrets(secrets).map(
    (s) => `${s.name} is not set (export ${s.env_var})`,
  );
}
