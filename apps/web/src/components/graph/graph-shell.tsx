"use client";

/**
 * The graph shell's dynamic-import boundary (TITAN-FRT-005, ADR-FRT-04).
 *
 * **Why this file exists.** ADR-FRT-04 names the heavy pair as "React Flow +
 * dagre", and `canvas.tsx` splits React Flow. Dagre was NOT split: the shell
 * computes layout itself — deliberately, because layout is a pure function and
 * computing it outside the canvas keeps it testable (see graph-shell-impl.tsx)
 * — and the shell was statically imported by both graph screens. So
 * `graph-shell-impl` → `lib/graph/layout` → `dagre` sat in the First Load JS of
 * `/projects/[pid]/graph` and `/projects/[pid]/architecture` at 30.8 kB
 * gzipped each, on two routes that already carry the most JS in the app.
 *
 * Half of the pair the ADR names had escaped the boundary that exists for it.
 * The `/org`-only bundle gate could not see it — nothing on /org imports a
 * graph — which is why Phase 66 also taught `check-bundle-budget.mjs` to assert
 * heavy modules are absent from EVERY route's first load, not just to total up
 * one route's bytes.
 *
 * **Why the boundary is here and not around the layout call.** Moving dagre
 * behind a lazy `import()` inside the shell would make a synchronous `useMemo`
 * asynchronous — and that memo holds positions across renders so a realtime
 * patch recolours in place instead of re-ranking the graph under the reader.
 * Splitting at the shell gets the same bytes out of first load without touching
 * that logic at all. Both screens already render `CanvasSkeleton` for their own
 * loading states, so the fallback is the frame they were showing anyway.
 *
 * `ssr: false` matches `canvas.tsx`: the shell's whole reason to exist is the
 * canvas, which measures the DOM with ResizeObserver on mount.
 */

import dynamic from "next/dynamic";

import { CanvasSkeleton } from "@/components/graph/canvas";
import type { GraphShellProps } from "@/components/graph/graph-shell-impl";

/**
 * `next/dynamic` erases the generic: it infers props from the module's export
 * and hands back a `ComponentType` fixed at one `D`. The cast restores the
 * call-site inference that `model`, `groupOf` and `onSelect` rely on to stay
 * type-checked together — the two screens pass different node data, and without
 * it one of them would silently widen to `unknown`.
 *
 * This is sound because `dynamic` changes only WHEN the component loads, never
 * which props it accepts. It is a cast rather than `any`: the shape is still
 * fully checked, and `tsc --strict` still rejects a wrong `groupOf`.
 */
export const GraphShell = dynamic(
  () => import("@/components/graph/graph-shell-impl").then((m) => m.GraphShell),
  { ssr: false, loading: () => <CanvasSkeleton /> },
) as <D>(props: GraphShellProps<D>) => React.ReactElement;

export type { GraphShellProps };
export type { ClusterData } from "@/components/graph/graph-shell-impl";
