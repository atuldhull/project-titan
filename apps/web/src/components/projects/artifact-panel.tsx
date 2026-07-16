"use client";

import Markdown from "react-markdown";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@titan/api-client";
import { useArtifact } from "@/lib/projects/hooks";

/**
 * Renders a Phase-51 derived artifact's Markdown (`content_md`) — roadmap, risk
 * analysis, tech-debt report. We read the existing artifact endpoint; nothing
 * here re-derives a roadmap.
 *
 * SECURITY: react-markdown does NOT render raw HTML unless `rehype-raw` is
 * added, so agent-authored Markdown cannot inject markup. Do not add rehype-raw
 * here without a sanitize allowlist (Vol 8 TITAN-SEC-072).
 *
 * A 404 is the normal "never generated" case, not a failure: artifacts are
 * produced by the H1->H4 handoff chain, so a young project legitimately has
 * none.
 */
export function ArtifactPanel({
  projectId,
  kind,
  title,
  emptyHint,
}: {
  projectId: string;
  kind: string;
  title: string;
  emptyHint: string;
}) {
  const { data, isLoading, isError, error, refetch } = useArtifact(
    projectId,
    kind,
  );
  const notGenerated = isError && error instanceof ApiError && error.isNotFound;

  return (
    <section className="rounded-lg border bg-card">
      <header className="flex items-center gap-2 border-b px-4 py-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {data ? (
          <Badge variant="secondary" className="ml-auto">
            v{data.artifact_version}
          </Badge>
        ) : null}
      </header>

      <div className="p-4">
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-4/6" />
          </div>
        ) : notGenerated ? (
          <p className="text-sm text-muted-foreground">{emptyHint}</p>
        ) : isError ? (
          <p className="text-sm text-destructive">
            Couldn’t load {title.toLowerCase()}.{" "}
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => void refetch()}
            >
              Retry
            </button>
          </p>
        ) : !data?.content_md?.trim() ? (
          <p className="text-sm text-muted-foreground">{emptyHint}</p>
        ) : (
          <div className="prose-titan max-w-none text-sm">
            <Markdown>{data.content_md}</Markdown>
          </div>
        )}
      </div>
    </section>
  );
}
