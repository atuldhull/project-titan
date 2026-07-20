"use client";

/**
 * The ADR side panel (§6.8: "Context/Decision/Alternatives/Trade-offs rendered
 * from backend records").
 *
 * There are TWO ADR sources in this backend and they are not the same record:
 *
 * 1. The `decisions` table — `{title, context, decision, alternatives,
 *    consequences, status, authority, supersedes_id}`. This is the one §6.8
 *    describes: all four fields exist. "Trade-offs" is stored as `consequences`
 *    — a rename, not a gap. This panel prefers it.
 * 2. `ArchitecturePackage.adrs` — only `{id, title, decision, rationale}`. No
 *    context, no alternatives. Shown only as a fallback, labelled, because a
 *    thinner record rendered under the same heading would imply the missing
 *    fields were empty rather than absent.
 *
 * Worth knowing: the Architect's prompt tells it to write ADRs via an
 * `adr.create` tool, and **that tool does not exist** (the grant in
 * `config/agents/architect.yaml` dangles — nothing validates tool grants). So in
 * practice the `decisions` table is populated by the conflict/arbitration paths,
 * and the Architect's own ADRs land in the artifact instead. Both are rendered;
 * neither is invented.
 */

import { useState } from "react";

import type { components } from "@titan/schemas";

import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useDecisions } from "@/lib/graph/hooks";
import type { PackageAdr } from "@/lib/graph/architecture";

type DecisionOut = components["schemas"]["DecisionOut"];

export function AdrPanel({
  projectId,
  packageAdrs,
}: {
  projectId: string;
  packageAdrs: readonly PackageAdr[];
}) {
  const decisions = useDecisions(projectId);
  const [open, setOpen] = useState<DecisionOut | null>(null);

  const records = decisions.data?.items ?? [];

  return (
    <aside className="space-y-2 rounded-lg border p-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        ADRs ({records.length + packageAdrs.length})
      </h3>

      {decisions.isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : decisions.isError ? (
        <p role="alert" className="text-sm text-destructive">
          Couldn’t load decision records.
        </p>
      ) : records.length === 0 && packageAdrs.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No decision records yet.
        </p>
      ) : (
        <ul className="space-y-1">
          {records.map((record) => (
            <li key={record.id}>
              <button
                type="button"
                onClick={() => setOpen(record)}
                // §8: ≥44 px touch targets under `md`. This is the only way into
                // a decision record, and at 375 px the whole panel is a stacked
                // list of them — a 30 px row here is a row that gets mis-tapped.
                className="w-full rounded-md border p-2 text-left text-sm hover:bg-muted max-md:min-h-11"
              >
                <span className="flex items-center gap-1">
                  <Badge
                    variant={
                      record.status === "superseded" ? "outline" : "secondary"
                    }
                    className="px-1 py-0 text-[9px]"
                  >
                    {record.status}
                  </Badge>
                  <span className="truncate font-medium">{record.title}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {packageAdrs.length > 0 ? (
        <div className="border-t pt-2">
          <p className="text-[10px] uppercase text-muted-foreground">
            From the architecture package
          </p>
          {/* Deliberately a separate list: these records have only a decision and
              a rationale — no context, no alternatives — so folding them in with
              the full ones would imply those fields were empty, not absent. */}
          <ul className="mt-1 space-y-1">
            {packageAdrs.map((adr) => (
              <li key={adr.id} className="rounded-md border p-2 text-sm">
                <div className="flex items-baseline gap-1">
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {adr.id}
                  </span>
                  <span className="truncate font-medium">{adr.title}</span>
                </div>
                <p className="mt-1 line-clamp-3 text-xs text-muted-foreground">
                  {adr.decision}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <Dialog open={open !== null} onOpenChange={(o) => !o && setOpen(null)}>
        <DialogContent className="max-h-[80vh] overflow-y-auto">
          {open ? (
            <>
              <DialogHeader>
                <DialogTitle>{open.title}</DialogTitle>
                <DialogDescription>
                  <Badge variant="secondary">{open.status}</Badge>
                  {open.authority ? (
                    <Badge variant="outline" className="ml-1">
                      {open.authority}
                    </Badge>
                  ) : null}
                </DialogDescription>
              </DialogHeader>
              <dl className="space-y-3 text-sm">
                <div>
                  <dt className="text-xs font-semibold uppercase text-muted-foreground">
                    Context
                  </dt>
                  <dd className="whitespace-pre-wrap">{open.context}</dd>
                </div>
                <div>
                  <dt className="text-xs font-semibold uppercase text-muted-foreground">
                    Decision
                  </dt>
                  <dd className="whitespace-pre-wrap">{open.decision}</dd>
                </div>
                <div>
                  <dt className="text-xs font-semibold uppercase text-muted-foreground">
                    Alternatives
                  </dt>
                  <dd className="whitespace-pre-wrap">{open.alternatives}</dd>
                </div>
                <div>
                  {/* §6.8 calls this "Trade-offs"; the column is `consequences`.
                      Same concept, and the heading follows the spec's word. */}
                  <dt className="text-xs font-semibold uppercase text-muted-foreground">
                    Trade-offs
                  </dt>
                  <dd className="whitespace-pre-wrap">{open.consequences}</dd>
                </div>
              </dl>
            </>
          ) : null}
        </DialogContent>
      </Dialog>
    </aside>
  );
}
