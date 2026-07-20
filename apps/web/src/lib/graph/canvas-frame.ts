/**
 * The canvas's frame class, shared by the real canvas and its skeleton (§8).
 *
 * These two MUST be the same box. `canvas.tsx` renders `CanvasSkeleton` while the
 * dynamic import resolves and `graph-canvas.tsx` renders the React Flow surface
 * after it does; if their heights disagree the whole page jumps at the moment the
 * chunk lands — the exact layout shift TITAN-FRT-041's "no horizontal body
 * scroll / usable at 375 px" pass is meant to catch. They lived as two separate
 * `h-[70vh]` literals in two files, which is a duplicate that only stays correct
 * by luck, so it is one constant now.
 *
 * Why the height is not a flat 70vh any more:
 *
 * - **Under `md` (§8, < 768).** The Architecture Viewer stacks its ADR panel
 *   BELOW the canvas at that width (`architecture-screen.tsx`), so a canvas that
 *   claims 70 % of a phone viewport pushes the entire panel past the fold with
 *   nothing on screen to suggest it exists. 55vh leaves the panel's heading
 *   visible, which is what makes it discoverable rather than merely present.
 * - **`min-h-80`.** A percentage height collapses on a short viewport (a landscape
 *   phone is ~375 px tall; 55 % of that is 206 px, which fits roughly one node).
 *   Below ~320 px a graph canvas is not a degraded view, it is an unusable one —
 *   so it stops shrinking and the page scrolls instead.
 */
export const CANVAS_FRAME_CLASS =
  "h-[70vh] min-h-80 w-full rounded-lg border max-md:h-[55vh]";
