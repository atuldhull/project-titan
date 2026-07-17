/**
 * §6.11's tier tree, mapped onto the tiers that actually exist.
 */
import { describe, expect, it } from "vitest";

import type { MemoryFacets } from "@titan/api-client";

import {
  buildTiers,
  emptyTierCopy,
  isProjectScope,
  pluralLabel,
  projectIdOf,
} from "@/lib/memory/tiers";

function facets(over: Partial<MemoryFacets> = {}): MemoryFacets {
  return {
    kinds: [],
    scopes: [],
    statuses: [],
    agents: [],
    subtypes: [],
    ...over,
  };
}

describe("pluralLabel", () => {
  it("humanises a snake_case subtype", () => {
    // The raw identifiers must not leak: `sprint_retro` was rendering as
    // "Sprint_retros".
    expect(pluralLabel("pattern")).toBe("Patterns");
    expect(pluralLabel("sprint_retro")).toBe("Sprint retros");
  });
});

describe("scope helpers", () => {
  it("recognises and unwraps a project scope", () => {
    expect(isProjectScope("org")).toBe(false);
    expect(isProjectScope("project:abc")).toBe(true);
    expect(projectIdOf("project:abc-123")).toBe("abc-123");
    expect(projectIdOf("org")).toBeNull();
  });
});

describe("buildTiers", () => {
  it("always offers Organization and Short-term, even with no data", () => {
    const tiers = buildTiers(facets());
    const ids = tiers.map((t) => t.id);
    expect(ids).toContain("scope:org");
    expect(ids).toContain("short-term");
  });

  it("marks Short-term unbrowsable rather than dropping it", () => {
    // It is real (Redis, 24h TTL) but has no query surface, so §6.11's tier is
    // shown present-but-disabled, not silently omitted.
    const shortTerm = buildTiers(facets()).find((t) => t.id === "short-term");
    expect(shortTerm?.kind).toBe("unavailable");
    expect(shortTerm?.filter).toBeNull();
    expect(shortTerm?.hint).toBeTruthy();
  });

  it("gives each project scope its own node with a real count", () => {
    const tiers = buildTiers(
      facets({
        scopes: [
          { value: "org", count: 5 },
          { value: "project:11111111-2222-3333-4444-555555555555", count: 3 },
        ],
      }),
    );
    const project = tiers.find(
      (t) => t.kind === "scope" && t.id.includes("project:"),
    );
    expect(project?.count).toBe(3);
    expect(project?.filter?.scope).toBe(
      "project:11111111-2222-3333-4444-555555555555",
    );
  });

  it("renders subtypes as sibling filter tiers with humanised labels", () => {
    const tiers = buildTiers(
      facets({ subtypes: [{ value: "sprint_retro", count: 4 }] }),
    );
    const subtype = tiers.find((t) => t.kind === "subtype");
    expect(subtype?.label).toBe("Sprint retros");
    expect(subtype?.filter).toEqual({ subtype: "sprint_retro" });
  });

  it("produces no duplicate ids", () => {
    const tiers = buildTiers(
      facets({
        scopes: [
          { value: "org", count: 1 },
          { value: "project:a", count: 1 },
        ],
        subtypes: [
          { value: "pattern", count: 1 },
          { value: "lesson", count: 1 },
        ],
      }),
    );
    expect(new Set(tiers.map((t) => t.id)).size).toBe(tiers.length);
  });
});

describe("emptyTierCopy", () => {
  it("explains a subtype tier in its own terms", () => {
    const tier = buildTiers(
      facets({ subtypes: [{ value: "pattern", count: 0 }] }),
    ).find((t) => t.kind === "subtype");
    expect(emptyTierCopy(tier ?? null).toLowerCase()).toContain("patterns");
  });

  it("falls back to a generic message for a plain scope", () => {
    expect(emptyTierCopy(null)).toMatch(/nothing/i);
  });
});
