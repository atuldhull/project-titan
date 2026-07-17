/**
 * §6.11's tier tree, mapped onto the tiers that actually exist.
 *
 * §6.11 draws five tiers — Short-term / Project / Org / Patterns / Lessons —
 * as if they were one axis. They are three different things:
 *
 * * **Org** and **Project** are real: `memory_records.scope` holds exactly
 *   `'org'` or `'project:<uuid>'`, and nothing else.
 * * **Patterns** and **Lessons** are NOT tiers. They are ordinary records
 *   discriminated by `metadata->>'record_subtype'`, so a record is BOTH
 *   (say) org-scoped AND a pattern — the two axes cross, they do not nest.
 * * **Short-term** is not in this table at all. It lives in Redis, keyed by
 *   task, with a 24h TTL (`memory/working.py`), and has no query surface.
 *
 * So the tree is rendered as what it is — two scope tiers, with the subtypes
 * as siblings that filter across them, and short-term shown as present but
 * unbrowsable rather than quietly dropped. A user who reads §6.11 and finds
 * four of five tiers should be told why, not left to wonder.
 */
import type { MemoryFacets } from "@titan/api-client";

export type TierKind = "scope" | "subtype" | "unavailable";

export interface Tier {
  readonly id: string;
  readonly label: string;
  readonly kind: TierKind;
  readonly count: number | null;
  /** The filter this tier applies, or null when it cannot be browsed. */
  readonly filter: {
    readonly scope?: string;
    readonly subtype?: string;
  } | null;
  readonly hint?: string;
}

/** The record kinds, which §6.11 never mentions but the model enforces. */
export const MEMORY_KINDS = ["episodic", "semantic", "procedural"] as const;

export function projectScope(projectId: string): string {
  return `project:${projectId}`;
}

export function isProjectScope(scope: string): boolean {
  return scope.startsWith("project:");
}

export function projectIdOf(scope: string): string | null {
  return isProjectScope(scope) ? scope.slice("project:".length) : null;
}

function countOf(
  facets: readonly { value: string; count: number }[],
  value: string,
): number {
  return facets.find((f) => f.value === value)?.count ?? 0;
}

/** `sprint_retro` -> `Sprint retros`: underscores to spaces, capitalised, `s`. */
export function pluralLabel(subtype: string): string {
  const spaced = subtype.replace(/_/g, " ");
  return `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}s`;
}

/**
 * Build the tree from the facet counts.
 *
 * Every project scope present in the data gets its own node, rather than a
 * hardcoded list: the scopes are whatever has been written, and inventing a
 * node for a project with no memory would be a tier that is empty by
 * construction.
 */
export function buildTiers(facets: MemoryFacets | undefined): Tier[] {
  const scopes = facets?.scopes ?? [];
  const subtypes = facets?.subtypes ?? [];

  const tiers: Tier[] = [
    {
      id: "scope:org",
      label: "Organization",
      kind: "scope",
      count: countOf(scopes, "org"),
      filter: { scope: "org" },
    },
    ...scopes
      .filter((s) => isProjectScope(s.value))
      .map((s): Tier => ({
        id: `scope:${s.value}`,
        label: `Project · ${projectIdOf(s.value)?.slice(0, 8)}`,
        kind: "scope",
        count: s.count,
        filter: { scope: s.value },
      })),
    ...subtypes.map((s): Tier => ({
      id: `subtype:${s.value}`,
      // `pattern` -> `Patterns`, `sprint_retro` -> `Sprint retros`. §6.11's own
      // words for these — and the subtypes are raw snake_case identifiers, so a
      // bare capitalise-and-append-s leaked `Sprint_retros`.
      label: pluralLabel(s.value),
      kind: "subtype",
      count: s.count,
      filter: { subtype: s.value },
    })),
    {
      id: "short-term",
      label: "Short-term",
      kind: "unavailable",
      count: null,
      filter: null,
      hint:
        "Short-term memory is per-task working state in Redis with a 24h TTL — " +
        "it is not a record tier and has no query surface.",
    },
  ];
  return tiers;
}

/** §6.11's per-tier empty copy. */
export function emptyTierCopy(tier: Tier | null): string {
  if (tier?.kind === "subtype") {
    return `Nothing stored here yet — ${tier.label.toLowerCase()} are written by the reflection and pattern miners after work completes.`;
  }
  if (tier?.id === "scope:org") {
    return "Nothing stored here yet — organization memory is promoted from project memory once a lesson generalises.";
  }
  return "Nothing stored here yet.";
}
