/**
 * Client-side SVG export (§6.8: "export current view as SVG/PNG, client-side,
 * offline-safe").
 *
 * Built from the LAID-OUT MODEL, not from the DOM. The first attempt cloned
 * React Flow's `.react-flow__edges` node, which was wrong twice over: in
 * @xyflow/react v12 that class is a `<div>` wrapping one `<svg>` per edge (a
 * v11 -> v12 change), so the "SVG" was a serialised div with a duplicate xmlns
 * that no viewer would open; and even had it worked, the node boxes are HTML
 * divs and would never have been in it — the file would have been arrows on an
 * empty background.
 *
 * Serialising the model instead is both correct and STRICTLY more honest: the
 * export contains the nodes AND the edges, matches what the canvas drew because
 * it comes from the same positions, and cannot be broken by a library upgrade
 * changing its internal markup. It is also pure, so it is genuinely tested.
 *
 * **Still SVG only, deliberately.** §6.8 also asks for PNG. Rasterising in the
 * browser means canvas + `drawImage` of a serialised SVG, which taints on some
 * paths and needs a font-embedding story to not silently drop every label. An
 * export that quietly loses text is worse than no PNG button, so there is none.
 */

import type { LaidOut } from "@/lib/graph/model";

export interface ExportResult {
  readonly ok: boolean;
  readonly reason?: string;
}

const PADDING = 24;

/**
 * The exported document's palette.
 *
 * TITAN-FRT-010 bans raw colours in app code and is right to: a component must
 * take its colour from a semantic token so the theme can move it. This is not
 * app code — it is a FILE being written out, which §6.8 requires to be
 * "offline-safe". A `var(--destructive)` in a downloaded SVG resolves against
 * nothing and renders black-on-black, so the one place a literal is correct is
 * exactly here. Kept in one named block so the exception is visible and bounded
 * rather than sprinkled through the markup.
 */
/* eslint-disable no-restricted-syntax -- see PALETTE above: a standalone SVG
   cannot reference CSS variables; these are document colours, not app styling. */
const PALETTE = {
  background: "#ffffff",
  nodeFill: "#ffffff",
  nodeStroke: "#cbd5e1",
  text: "#0f172a",
  edge: "#94a3b8",
  blockedEdge: "#b91c1c",
} as const;
/* eslint-enable no-restricted-syntax */

function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Render the laid-out graph as standalone SVG markup.
 *
 * Deliberately self-contained: no CSS variables, no external fonts, no
 * stylesheet reference — "offline-safe" means the file opens correctly with
 * nothing else present, so colours are literal and the font is a generic family.
 * (TITAN-FRT-010 bans raw colours in the APP's styling, where a token must win;
 * this is a document being written out, not a component being styled.)
 */
export function toSvg(laid: LaidOut<unknown>, title: string): string {
  const nodes = laid.nodes;
  if (nodes.length === 0) {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><title>${escapeXml(title)}</title></svg>`;
  }
  const minX = Math.min(...nodes.map((n) => n.x)) - PADDING;
  const minY = Math.min(...nodes.map((n) => n.y)) - PADDING;
  const maxX = Math.max(...nodes.map((n) => n.x + n.width)) + PADDING;
  const maxY = Math.max(...nodes.map((n) => n.y + n.height)) + PADDING;
  const width = maxX - minX;
  const height = maxY - minY;
  const at = new Map(nodes.map((n) => [n.id, n]));

  const edges = laid.edges
    .map((edge) => {
      const from = at.get(edge.from);
      const to = at.get(edge.to);
      if (!from || !to) {
        return "";
      }
      const x1 = from.x + from.width;
      const y1 = from.y + from.height / 2;
      const x2 = to.x;
      const y2 = to.y + to.height / 2;
      const dash = edge.blocked ? ' stroke-dasharray="4 3"' : "";
      const stroke = edge.blocked ? PALETTE.blockedEdge : PALETTE.edge;
      return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" stroke-width="1.5"${dash} marker-end="url(#arrow)" />`;
    })
    .join("\n    ");

  const boxes = nodes
    .map(
      (node) =>
        `<g><rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.height}" rx="6" fill="${PALETTE.nodeFill}" stroke="${PALETTE.nodeStroke}" />` +
        `<text x="${node.x + 8}" y="${node.y + node.height / 2 + 4}" font-family="sans-serif" font-size="11" fill="${PALETTE.text}">${escapeXml(
          node.label.length > 26 ? `${node.label.slice(0, 25)}…` : node.label,
        )}</text></g>`,
    )
    .join("\n    ");

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${minX} ${minY} ${width} ${height}" width="${width}" height="${height}">`,
    `  <title>${escapeXml(title)}</title>`,
    `  <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">`,
    `    <path d="M 0 0 L 10 5 L 0 10 z" fill="${PALETTE.edge}" /></marker></defs>`,
    `  <rect x="${minX}" y="${minY}" width="${width}" height="${height}" fill="${PALETTE.background}" />`,
    `    ${edges}`,
    `    ${boxes}`,
    `</svg>`,
  ].join("\n");
}

/**
 * Serialise the graph and trigger a download.
 *
 * `doc` is injectable so the whole path is testable in jsdom, and the result is
 * RETURNED rather than thrown — the caller surfaces a failure, because a button
 * that silently does nothing is indistinguishable from a broken one.
 */
export function exportSvg(
  laid: LaidOut<unknown>,
  filename: string,
  doc: Document | undefined = typeof document === "undefined"
    ? undefined
    : document,
): ExportResult {
  if (!doc) {
    return { ok: false, reason: "no document" };
  }
  if (laid.nodes.length === 0) {
    return { ok: false, reason: "nothing to export" };
  }
  const blob = new Blob([toSvg(laid, filename)], {
    type: "image/svg+xml;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = doc.createElement("a");
  anchor.href = url;
  anchor.download = `${filename}.svg`;
  doc.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking synchronously can race the download in some browsers; a task later
  // is after the click has been dispatched.
  setTimeout(() => URL.revokeObjectURL(url), 0);
  return { ok: true };
}
