"use client";

/**
 * The selected record: content, embedding meta, provenance chain, usage
 * history, and the curation actions §6.11 asks for (TITAN-FRT-034).
 */
import Link from "next/link";
import { Archive, Pin, Tag } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useMemoryRecord } from "@/lib/memory/hooks";
import {
  provenanceComplete,
  provenanceOf,
  usageHistory,
} from "@/lib/memory/provenance";

export function MemoryDetail({
  recordId,
}: {
  readonly recordId: string | null;
}) {
  const detail = useMemoryRecord(recordId);

  if (recordId === null) {
    return (
      <p className="p-6 text-xs text-muted-foreground">
        Select a record to see its content, provenance and usage history.
      </p>
    );
  }
  if (detail.isLoading) {
    return (
      <div className="space-y-2 p-3">
        <Skeleton className="h-6 w-3/4" />
        <Skeleton className="h-24" />
        <Skeleton className="h-16" />
      </div>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <p className="p-6 text-xs text-destructive">Couldn’t load this record.</p>
    );
  }

  const { record, chunks } = detail.data;
  const links = provenanceOf(detail.data);
  const history = usageHistory(detail.data);
  const reads = history.filter((e) => e.action === "read");

  return (
    <div className="space-y-4 p-3 text-xs">
      <header className="space-y-1">
        <h2 className="text-sm font-semibold">{record.title}</h2>
        <div className="flex flex-wrap gap-1">
          <Badge variant="outline" className="h-4 px-1 text-[10px]">
            {record.kind}
          </Badge>
          <Badge variant="secondary" className="h-4 px-1 font-mono text-[10px]">
            {record.scope}
          </Badge>
          {record.status !== "active" ? (
            <Badge variant="destructive" className="h-4 px-1 text-[10px]">
              {record.status}
            </Badge>
          ) : null}
        </div>
      </header>

      <section className="space-y-1">
        <h3 className="font-medium text-muted-foreground">Content</h3>
        <p className="max-h-40 overflow-auto whitespace-pre-wrap rounded border bg-muted/40 p-2 font-mono text-[11px]">
          {record.body_md}
        </p>
      </section>

      <section className="space-y-1">
        <h3 className="font-medium text-muted-foreground">Embedding</h3>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          <dt className="text-muted-foreground">chunks</dt>
          <dd>{chunks.length}</dd>
          <dt className="text-muted-foreground">tokens</dt>
          <dd>{chunks.reduce((n, c) => n + c.token_count, 0)}</dd>
          <dt className="text-muted-foreground">model</dt>
          <dd className="font-mono">
            {chunks[0]?.embed_model ?? (
              <span className="text-muted-foreground">not embedded yet</span>
            )}
          </dd>
          <dt className="text-muted-foreground">importance</dt>
          <dd className="tabular-nums">{record.importance.toFixed(2)}</dd>
          <dt className="text-muted-foreground">decay</dt>
          <dd className="tabular-nums">{record.decay_score.toFixed(2)}</dd>
        </dl>
      </section>

      {/* TITAN-FRT-034's MUST: creating agent, source entity, timestamp. */}
      <section className="space-y-1">
        <h3 className="font-medium text-muted-foreground">Provenance</h3>
        <p className="text-[11px] text-muted-foreground">
          Written {new Date(record.created_at).toLocaleString()}
        </p>
        <ul className="space-y-1">
          {links.map((link) => (
            <li key={`${link.kind}:${link.label}`}>
              {link.href ? (
                <Button
                  asChild
                  size="sm"
                  variant="outline"
                  className="h-6 px-2 text-[11px]"
                >
                  <Link href={link.href}>{link.label}</Link>
                </Button>
              ) : (
                // Real reference, nothing to resolve it to. Named with the
                // reason rather than rendered as a dead link.
                <span
                  className="inline-flex h-6 items-center rounded border border-dashed px-2 text-[11px] text-muted-foreground"
                  title={link.reason}
                >
                  {link.label}
                </span>
              )}
            </li>
          ))}
        </ul>
        {!provenanceComplete(detail.data) ? (
          <p className="text-[10px] text-muted-foreground">
            No source task recorded — this record names its author and its time,
            but not the work it came from.
          </p>
        ) : null}
      </section>

      <section className="space-y-1">
        <h3 className="font-medium text-muted-foreground">
          Usage history · {reads.length} read{reads.length === 1 ? "" : "s"}
        </h3>
        {history.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            Never retrieved. The ledger is append-only, so this is the complete
            trail, not a sample.
          </p>
        ) : (
          <ul className="max-h-40 space-y-0.5 overflow-auto font-mono text-[10px]">
            {history.map((e, i) => (
              <li key={`${e.at}-${i}`} className="flex gap-2">
                <span className="shrink-0 text-muted-foreground">
                  {new Date(e.at).toLocaleString()}
                </span>
                <span className="shrink-0">{e.action}</span>
                <span className="truncate text-muted-foreground">
                  {e.actor}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-1 border-t pt-2">
        <div className="flex flex-wrap gap-1">
          {/* §6.11 asks for Pin / Edit tags / Archive. There is no per-record
              endpoint for any of them — curation is a batch job that decays and
              prunes on its own schedule, and pin/archive are column states no
              route can set. Disabled with the reason beats a button that
              silently does nothing. */}
          <Button
            size="sm"
            variant="outline"
            disabled
            className="h-6 gap-1 px-2 text-[11px]"
          >
            <Pin className="h-3 w-3" aria-hidden />
            Pin
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled
            className="h-6 gap-1 px-2 text-[11px]"
          >
            <Tag className="h-3 w-3" aria-hidden />
            Edit tags
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled
            className="h-6 gap-1 px-2 text-[11px]"
          >
            <Archive className="h-3 w-3" aria-hidden />
            Archive
          </Button>
        </div>
        <p className="text-[10px] text-muted-foreground">
          Curation runs as a scheduled batch (decay, prune, promote). There is
          no per-record pin/tag/archive endpoint to call from here yet.
        </p>
      </section>
    </div>
  );
}
