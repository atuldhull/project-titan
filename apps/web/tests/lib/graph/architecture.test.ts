/**
 * The architecture model + the version diff (Vol 4 §6.8, TITAN-FRT-031).
 */

import { describe, expect, it } from "vitest";

import {
  architectureModel,
  diffArchitecture,
  diffModel,
  layerGroup,
  parsePackage,
  type ArchitecturePackage,
} from "@/lib/graph/architecture";

function pkg(
  components: readonly [string, string, string[]][],
): ArchitecturePackage {
  return {
    components: components.map(([name, layer, depends_on]) => ({
      name,
      layer,
      depends_on,
    })),
  };
}

describe("parsePackage", () => {
  it("reads a real ArchitecturePackage body", () => {
    const parsed = parsePackage({
      artifact_type: "ArchitecturePackage",
      component_diagram_ascii: "[r]->[uc]",
      components: [
        { name: "ContactEntity", layer: "entities", depends_on: [] },
        {
          name: "AddContact",
          layer: "use_cases",
          depends_on: ["ContactEntity"],
        },
      ],
      adrs: [
        { id: "ADR-1", title: "Use RS256", decision: "d", rationale: "r" },
      ],
    });
    expect(parsed?.components).toHaveLength(2);
    expect(parsed?.adrs?.[0].id).toBe("ADR-1");
    expect(parsed?.component_diagram_ascii).toBe("[r]->[uc]");
  });

  it("returns null rather than throwing on a body that is not a package", () => {
    // `body` is JSONB typed `{type: object, additionalProperties: true}` in
    // OpenAPI — nothing after submit-time guarantees the shape, and a malformed
    // one must render an empty architecture, not crash the route.
    expect(parsePackage(null)).toBeNull();
    expect(parsePackage("nope")).toBeNull();
    expect(parsePackage({ components: "not an array" })).toBeNull();
  });

  it("skips malformed components instead of failing the whole package", () => {
    const parsed = parsePackage({
      components: [
        { name: "Good", layer: "entities", depends_on: [] },
        { layer: "entities" },
        42,
      ],
    });
    expect(parsed?.components.map((c) => c.name)).toEqual(["Good"]);
  });
});

describe("architectureModel", () => {
  it("builds nodes and edges from components + depends_on", () => {
    const model = architectureModel(
      pkg([
        ["Entity", "entities", []],
        ["UseCase", "use_cases", ["Entity"]],
      ]),
    );
    expect(model.nodes.map((n) => n.id)).toEqual(["Entity", "UseCase"]);
    // depends_on means "UseCase needs Entity" -> the edge flows Entity -> UseCase.
    expect(model.edges).toEqual([{ from: "Entity", to: "UseCase" }]);
  });

  it("drops a depends_on that names a component not in the package", () => {
    // `depends_on` is free text an agent wrote; nothing validates it. A dangling
    // name must not materialise a node nobody can inspect.
    const model = architectureModel(pkg([["UseCase", "use_cases", ["Ghost"]]]));
    expect(model.nodes.map((n) => n.id)).toEqual(["UseCase"]);
    expect(model.edges).toEqual([]);
  });

  it("is empty for an absent package", () => {
    expect(architectureModel(null)).toEqual({ nodes: [], edges: [] });
  });

  it("groups by the REAL vocabulary — Clean layers, not C4", () => {
    const model = architectureModel(
      pkg([
        ["E", "entities", []],
        ["U", "", []],
      ]),
    );
    expect(layerGroup(model.nodes[0])).toBe("entities");
    expect(layerGroup(model.nodes[1])).toBe("unclassified");
  });
});

describe("diffArchitecture (TITAN-FRT-031)", () => {
  it("marks an added component", () => {
    const diff = diffArchitecture(
      pkg([["A", "entities", []]]),
      pkg([
        ["A", "entities", []],
        ["B", "use_cases", []],
      ]),
    );
    expect(diff.added).toEqual(["B"]);
    expect(diff.marks.get("B")).toBe("added");
  });

  it("marks a removed component", () => {
    const diff = diffArchitecture(
      pkg([
        ["A", "entities", []],
        ["B", "use_cases", []],
      ]),
      pkg([["A", "entities", []]]),
    );
    expect(diff.removed).toEqual(["B"]);
    expect(diff.marks.get("B")).toBe("removed");
  });

  it("marks a component whose layer moved", () => {
    const diff = diffArchitecture(
      pkg([["A", "entities", []]]),
      pkg([["A", "use_cases", []]]),
    );
    expect(diff.changed).toEqual(["A"]);
    expect(diff.marks.get("A")).toBe("changed");
  });

  it("marks a component whose dependencies moved", () => {
    const diff = diffArchitecture(
      pkg([
        ["A", "entities", []],
        ["B", "use_cases", []],
      ]),
      pkg([
        ["A", "entities", []],
        ["B", "use_cases", ["A"]],
      ]),
    );
    expect(diff.changed).toEqual(["B"]);
  });

  it("does not mark a dependency list that was merely reordered", () => {
    // Order is not architecture. Amber here would point at a difference the
    // canvas cannot show.
    const diff = diffArchitecture(
      pkg([
        ["A", "e", []],
        ["B", "e", []],
        ["C", "u", ["A", "B"]],
      ]),
      pkg([
        ["A", "e", []],
        ["B", "e", []],
        ["C", "u", ["B", "A"]],
      ]),
    );
    expect(diff.changed).toEqual([]);
  });

  it("reports nothing for two identical versions", () => {
    const same = pkg([
      ["A", "entities", ["B"]],
      ["B", "use_cases", []],
    ]);
    const diff = diffArchitecture(same, same);
    expect(diff).toMatchObject({ added: [], removed: [], changed: [] });
    expect(diff.marks.size).toBe(0);
  });

  it("treats a first version as all-added", () => {
    const diff = diffArchitecture(null, pkg([["A", "entities", []]]));
    expect(diff.added).toEqual(["A"]);
  });
});

describe("diffModel", () => {
  it("still draws a removed component — §6.8's red ghost", () => {
    // Omitting it would make the diff identical to the newer version, which is
    // the one thing a diff must never be.
    const model = diffModel(
      pkg([
        ["A", "entities", []],
        ["Gone", "use_cases", []],
      ]),
      pkg([["A", "entities", []]]),
    );
    expect(model.nodes.map((n) => n.id).sort()).toEqual(["A", "Gone"]);
    expect(model.nodes.find((n) => n.id === "Gone")?.data.diff).toBe("removed");
  });

  it("carries every mark onto the nodes it draws", () => {
    const model = diffModel(
      pkg([
        ["Keep", "entities", []],
        ["Drop", "entities", []],
        ["Move", "entities", []],
      ]),
      pkg([
        ["Keep", "entities", []],
        ["Move", "use_cases", []],
        ["New", "frameworks", []],
      ]),
    );
    const marks = Object.fromEntries(
      model.nodes.map((n) => [n.id, n.data.diff ?? "unchanged"]),
    );
    expect(marks).toEqual({
      Keep: "unchanged",
      Move: "changed",
      New: "added",
      Drop: "removed",
    });
  });
});
