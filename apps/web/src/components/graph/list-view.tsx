"use client";

/**
 * The accessible alternative to the canvas (TITAN-FRT-047).
 *
 * "Graph/Architecture canvases provide an accessible alternative: a structured
 * list view of nodes/edges toggleable via 'List view'... list exposes same data
 * and actions."
 *
 * This is not a fallback or a degraded mode — it is the same model, and the same
 * selection callback, rendered as a table a screen reader can actually read. A
 * React Flow canvas is a pile of absolutely-positioned divs joined by SVG paths;
 * the edges exist only as geometry, so "what does this depend on" is
 * unanswerable without sight. Here it is a column.
 */

import { Badge } from "@/components/ui/badge";
import { adjacency, type GraphModel } from "@/lib/graph/model";

export function ListView({
  model,
  selectedId,
  onSelect,
  onActivate,
  activateLabel,
  emptyLabel,
}: {
  model: GraphModel<unknown>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** The canvas's double-click action — FRT-047 says the list offers it too. */
  onActivate?: (id: string) => void;
  /** Names that action. Absent = the row offers selection only. */
  activateLabel?: string;
  emptyLabel: string;
}) {
  const adj = adjacency(model);

  if (model.nodes.length === 0) {
    return (
      <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        {emptyLabel}
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border">
      {/* A real <table>: TITAN-FRT-043 requires tables be tables. */}
      <table className="w-full text-sm">
        <caption className="sr-only">
          {model.nodes.length} nodes and {model.edges.length} edges. Each row
          lists what a node depends on and what depends on it.
        </caption>
        <thead className="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Node
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Depends on
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Required by
            </th>
          </tr>
        </thead>
        <tbody>
          {model.nodes.map((node) => {
            const label = (id: string) =>
              model.nodes.find((n) => n.id === id)?.label ?? id;
            const upstream = adj.in.get(node.id) ?? [];
            const downstream = adj.out.get(node.id) ?? [];
            return (
              <tr
                key={node.id}
                className={selectedId === node.id ? "bg-accent" : "border-t"}
              >
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  <button
                    type="button"
                    onClick={() => onSelect(node.id)}
                    aria-current={selectedId === node.id ? "true" : undefined}
                    className="text-left font-medium underline-offset-2 hover:underline"
                  >
                    {node.label}
                  </button>
                  {onActivate && activateLabel ? (
                    <button
                      type="button"
                      onClick={() => onActivate(node.id)}
                      className="ml-2 text-xs text-muted-foreground underline"
                    >
                      {activateLabel}
                    </button>
                  ) : null}
                </th>
                <td className="px-3 py-2 text-muted-foreground">
                  {upstream.length === 0 ? (
                    <span className="text-xs">Nothing</span>
                  ) : (
                    <span className="flex flex-wrap gap-1">
                      {upstream.map((id) => (
                        <Badge
                          key={id}
                          variant="secondary"
                          className="text-[10px]"
                        >
                          {label(id)}
                        </Badge>
                      ))}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-muted-foreground">
                  {downstream.length === 0 ? (
                    <span className="text-xs">Nothing</span>
                  ) : (
                    <span className="flex flex-wrap gap-1">
                      {downstream.map((id) => (
                        <Badge
                          key={id}
                          variant="secondary"
                          className="text-[10px]"
                        >
                          {label(id)}
                        </Badge>
                      ))}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
