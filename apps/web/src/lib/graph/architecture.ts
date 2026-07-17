/**
 * The architecture as the shared engine sees it (Vol 4 §6.8).
 *
 * **Read this before trusting §6.8.** The backend has no architecture model —
 * 51 tables, none architectural. What exists is ONE artifact: the H5
 * `ArchitecturePackage`, stored as an opaque JSONB body on `handoff_artifacts`.
 * Three consequences shape everything here:
 *
 * 1. **The node types are Clean Architecture layers, not C4.** The real vocabulary
 *    is `entities | use_cases | interface_adapters | frameworks`
 *    (`handoffs/schemas.py: CleanLayer`). §6.8's `system/container/component/
 *    datastore/external` exists nowhere in the backend, and neither does its
 *    Context→Container→Component switcher: the package is SINGLE-LEVEL. Rendering
 *    C4 would mean inventing a classification the Architect never made. The level
 *    switcher becomes a LAYER filter, which is the same affordance over the
 *    vocabulary that actually exists.
 * 2. **The body is untyped in OpenAPI** (`body: {type: object, additionalProperties:
 *    true}`), so these types are hand-written — the one place in this app where
 *    that is unavoidable. They mirror `handoffs/schemas.py` exactly; parsing is
 *    defensive because nothing but the submit-time gate guarantees the shape.
 * 3. **Versions are real.** Every Architect resubmission is a preserved row
 *    (`UNIQUE(project_id, stage, version)`), which is what makes the timeline and
 *    the diff below genuine rather than theatre.
 *
 * Pure: no React, no fetch.
 */

import type { GraphModel, GraphNode } from "@/lib/graph/model";

/** `handoffs/schemas.py: CleanLayer` — the REAL node vocabulary. */
export const LAYERS = [
  "entities",
  "use_cases",
  "interface_adapters",
  "frameworks",
] as const;
export type Layer = (typeof LAYERS)[number];

/** `handoffs/schemas.py: Component`. */
export interface Component {
  readonly name: string;
  readonly layer: string;
  readonly depends_on: readonly string[];
}

/** `handoffs/schemas.py: Adr` — NOT the `decisions` table's richer record. */
export interface PackageAdr {
  readonly id: string;
  readonly title: string;
  readonly decision: string;
  readonly rationale: string;
}

/** The subset of `ArchitecturePackage` this screen renders. */
export interface ArchitecturePackage {
  readonly components: readonly Component[];
  readonly component_diagram_ascii?: string;
  readonly adrs?: readonly PackageAdr[];
}

export interface ComponentNodeData {
  readonly layer: string;
  /** Only set in diff mode (§6.8: added=green, removed=red ghost, changed=amber). */
  readonly diff?: DiffMark;
}

export type DiffMark = "added" | "removed" | "changed";

/**
 * Narrow an artifact `body` into a package. Never throws: the body is JSONB that
 * only the submit-time gate ever validated, and a malformed one must render an
 * empty architecture, not crash the route.
 */
export function parsePackage(body: unknown): ArchitecturePackage | null {
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const b = body as Record<string, unknown>;
  if (!Array.isArray(b.components)) {
    return null;
  }
  const components: Component[] = [];
  for (const raw of b.components) {
    if (typeof raw !== "object" || raw === null) {
      continue;
    }
    const c = raw as Record<string, unknown>;
    if (typeof c.name !== "string") {
      continue;
    }
    components.push({
      name: c.name,
      layer: typeof c.layer === "string" ? c.layer : "",
      depends_on: Array.isArray(c.depends_on)
        ? c.depends_on.filter((d): d is string => typeof d === "string")
        : [],
    });
  }
  const adrs: PackageAdr[] = Array.isArray(b.adrs)
    ? b.adrs.flatMap((raw) => {
        if (typeof raw !== "object" || raw === null) {
          return [];
        }
        const a = raw as Record<string, unknown>;
        return typeof a.id === "string" && typeof a.title === "string"
          ? [
              {
                id: a.id,
                title: a.title,
                decision: typeof a.decision === "string" ? a.decision : "",
                rationale: typeof a.rationale === "string" ? a.rationale : "",
              },
            ]
          : [];
      })
    : [];
  return {
    components,
    component_diagram_ascii:
      typeof b.component_diagram_ascii === "string"
        ? b.component_diagram_ascii
        : undefined,
    adrs,
  };
}

/**
 * Build the engine's model from a package.
 *
 * Components are keyed by NAME — that is the only identity the artifact has, and
 * `depends_on` is a list of names. A `depends_on` naming a component that is not
 * in the package is dropped: the Architect wrote free text and nothing validates
 * it, so a dangling reference is a real possibility and must not become a node.
 */
export function architectureModel(
  pkg: ArchitecturePackage | null,
  marks: ReadonlyMap<string, DiffMark> = new Map(),
): GraphModel<ComponentNodeData> {
  if (!pkg) {
    return { nodes: [], edges: [] };
  }
  const known = new Set(pkg.components.map((c) => c.name));
  return {
    nodes: pkg.components.map((component) => ({
      id: component.name,
      kind: "component",
      label: component.name,
      data: { layer: component.layer, diff: marks.get(component.name) },
    })),
    edges: pkg.components.flatMap((component) =>
      component.depends_on
        .filter((dep) => known.has(dep))
        .map((dep) => ({ from: dep, to: component.name })),
    ),
  };
}

/** Group key for the clustering guard: the component's layer. */
export function layerGroup(node: GraphNode<ComponentNodeData>): string {
  return node.data.layer || "unclassified";
}

// --- the diff (TITAN-FRT-031) ----------------------------------------------

export interface ArchitectureDiff {
  /** Present in `to`, absent from `from` — green outline. */
  readonly added: readonly string[];
  /** Present in `from`, absent from `to` — red ghost (still drawn). */
  readonly removed: readonly string[];
  /** In both, but its layer or dependencies moved — amber. */
  readonly changed: readonly string[];
  readonly marks: ReadonlyMap<string, DiffMark>;
}

/**
 * Diff two architecture versions (§6.8: added=green outline, removed=red ghost,
 * changed=amber).
 *
 * "Changed" means the component's LAYER or its DEPENDENCIES differ. Those are
 * the only fields the canvas draws, so they are the only ones a visual diff can
 * honestly claim: two versions whose components differ solely in, say, an API
 * contract are identical ON THIS CANVAS, and marking them amber would point at a
 * difference the user cannot see. Dependencies compare as a SET — reordering a
 * list is not an architecture change.
 *
 * Both arguments are full package bodies, which is what the version endpoints
 * now make fetchable; before them a diff was impossible, not merely unbuilt.
 */
export function diffArchitecture(
  from: ArchitecturePackage | null,
  to: ArchitecturePackage | null,
): ArchitectureDiff {
  const before = new Map((from?.components ?? []).map((c) => [c.name, c]));
  const after = new Map((to?.components ?? []).map((c) => [c.name, c]));

  const added: string[] = [];
  const removed: string[] = [];
  const changed: string[] = [];
  const marks = new Map<string, DiffMark>();

  for (const [name, component] of after) {
    const old = before.get(name);
    if (!old) {
      added.push(name);
      marks.set(name, "added");
    } else if (
      old.layer !== component.layer ||
      !sameSet(old.depends_on, component.depends_on)
    ) {
      changed.push(name);
      marks.set(name, "changed");
    }
  }
  for (const name of before.keys()) {
    if (!after.has(name)) {
      removed.push(name);
      marks.set(name, "removed");
    }
  }
  return { added, removed, changed, marks };
}

/**
 * True when two dependency lists name the same SET of components.
 *
 * Compares set-to-set in both directions. Length-plus-one-way-containment is the
 * tempting shortcut and it is wrong: `["A","A"]` vs `["A","B"]` matches on length
 * and every element of the second is... not in the first, but reverse the case —
 * `["A","B"]` vs `["A","A"]` — and one-way containment passes while the
 * dependency genuinely changed. `depends_on` is agent-written free text with no
 * uniqueness constraint, so duplicates are possible and this must not miss them.
 */
function sameSet(a: readonly string[], b: readonly string[]): boolean {
  const left = new Set(a);
  const right = new Set(b);
  if (left.size !== right.size) {
    return false;
  }
  for (const value of left) {
    if (!right.has(value)) {
      return false;
    }
  }
  return true;
}

/**
 * The model to DRAW in diff mode: every component from both versions.
 *
 * A removed component must still be rendered — §6.8 calls it a "red ghost", and
 * a diff that simply omitted it would be indistinguishable from the newer
 * version. So the union is drawn, with the older body supplying the ghosts.
 */
export function diffModel(
  from: ArchitecturePackage | null,
  to: ArchitecturePackage | null,
): GraphModel<ComponentNodeData> {
  const diff = diffArchitecture(from, to);
  const union: Component[] = [...(to?.components ?? [])];
  const present = new Set(union.map((c) => c.name));
  for (const component of from?.components ?? []) {
    if (!present.has(component.name)) {
      union.push(component);
    }
  }
  return architectureModel({ components: union }, diff.marks);
}
