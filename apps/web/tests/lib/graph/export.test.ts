/**
 * Client-side SVG export (§6.8).
 *
 * The first version of this test is the cautionary tale: it hand-built
 * `<svg class="react-flow__edges">` and passed, while the app — on @xyflow/react
 * v12, where that class is a `<div>` — wrote a malformed file for every user. The
 * fixture encoded the false premise, so the fixture was what was green.
 *
 * The export now serialises the laid-out MODEL, so there is no library DOM to
 * mis-assume: these tests parse the real output as XML and assert what is in it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { exportSvg, toSvg } from "@/lib/graph/export";
import type { LaidOut } from "@/lib/graph/model";

const LAID: LaidOut<unknown> = {
  nodes: [
    {
      id: "a",
      kind: "task",
      label: "Schema DB",
      data: {},
      x: 0,
      y: 0,
      width: 180,
      height: 52,
    },
    {
      id: "b",
      kind: "task",
      label: "Auth API",
      data: {},
      x: 244,
      y: 0,
      width: 180,
      height: 52,
    },
  ],
  edges: [{ from: "a", to: "b", blocked: true }],
};

describe("toSvg", () => {
  it("produces well-formed, standalone XML", () => {
    // The whole point: the previous implementation emitted a div with two xmlns
    // attributes, which every viewer rejects with a parse error.
    const markup = toSvg(LAID, "architecture-v3");
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
    expect(doc.querySelector("parsererror")).toBeNull();
    expect(doc.documentElement.tagName).toBe("svg");
    expect(doc.documentElement.namespaceURI).toBe("http://www.w3.org/2000/svg");
  });

  it("contains the NODES, not just the arrows", () => {
    // React Flow draws nodes as HTML divs, so any DOM-scraping export would have
    // shipped edges on an empty background. Serialising the model is what makes
    // the file a picture of the diagram.
    const doc = new DOMParser().parseFromString(
      toSvg(LAID, "x"),
      "image/svg+xml",
    );
    const labels = [...doc.querySelectorAll("text")].map((t) => t.textContent);
    expect(labels).toEqual(["Schema DB", "Auth API"]);
    expect(doc.querySelectorAll("line")).toHaveLength(1);
  });

  it("dashes a blocked edge, as the canvas does", () => {
    const doc = new DOMParser().parseFromString(
      toSvg(LAID, "x"),
      "image/svg+xml",
    );
    expect(doc.querySelector("line")?.getAttribute("stroke-dasharray")).toBe(
      "4 3",
    );
  });

  it("escapes XML in a label rather than emitting broken markup", () => {
    // Task titles are agent-authored free text.
    const markup = toSvg(
      {
        nodes: [
          {
            id: "a",
            kind: "task",
            label: 'A & B <script>"x"',
            data: {},
            x: 0,
            y: 0,
            width: 180,
            height: 52,
          },
        ],
        edges: [],
      },
      "x",
    );
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
    expect(doc.querySelector("parsererror")).toBeNull();
    expect(doc.querySelector("text")?.textContent).toContain("A & B <script>");
  });

  it("sizes the viewBox around the content", () => {
    const doc = new DOMParser().parseFromString(
      toSvg(LAID, "x"),
      "image/svg+xml",
    );
    // Nodes span x 0..424, y 0..52, plus 24 padding each side.
    expect(doc.documentElement.getAttribute("viewBox")).toBe("-24 -24 472 100");
  });

  it("drops an edge whose endpoint is not in the model", () => {
    const doc = new DOMParser().parseFromString(
      toSvg({ nodes: LAID.nodes, edges: [{ from: "a", to: "ghost" }] }, "x"),
      "image/svg+xml",
    );
    expect(doc.querySelectorAll("line")).toHaveLength(0);
  });
});

describe("exportSvg", () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => "blob:fake");
    URL.revokeObjectURL = vi.fn();
  });

  it("downloads the graph under the given name", () => {
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this.download);
    });
    expect(exportSvg(LAID, "architecture-v3")).toEqual({ ok: true });
    expect(clicked).toEqual(["architecture-v3.svg"]);
    vi.restoreAllMocks();
  });

  it("reports a reason instead of writing an empty file", () => {
    // The caller toasts this — a button that silently does nothing is
    // indistinguishable from a broken one.
    expect(exportSvg({ nodes: [], edges: [] }, "x")).toEqual({
      ok: false,
      reason: "nothing to export",
    });
  });
});
