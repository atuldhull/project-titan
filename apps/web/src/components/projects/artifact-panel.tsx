"use client";

import Markdown, { type Components } from "react-markdown";

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

/**
 * TITAN-FRT-043 ("tables as tables") — the agent-authored Markdown in a roadmap
 * or risk register is full of tables, and they were losing their table role.
 *
 * `.prose-titan table` in globals.css sets `display: block; overflow-x: auto` to
 * stop wide tables scrolling the page body (§8). It works visually and is a
 * total accessibility failure: `display: block` on a `<table>` DESTROYS the
 * generated table box in every major browser, so the element stops mapping to
 * the table role and its rows and cells stop mapping to row/cell — a screen
 * reader gets an undifferentiated run of text with no column headers and no
 * grid navigation, which is precisely the defect this requirement names.
 *
 * The fix is structural rather than cosmetic: put the scroll port on a WRAPPER
 * <div> and let the <table> stay `display: table`. `overflow-x-auto` here beats
 * the stylesheet on the wrapper, and the inline `display: table` beats
 * `.prose-titan table` on the element (inline styles outrank any selector), so
 * this is correct even while the globals.css rule is still present — see the
 * report, that rule's `display: block` should be deleted.
 *
 * HONEST CAVEAT: this override is currently DORMANT. Pipe tables are a GFM
 * extension, `remark-gfm` is not a dependency of this app and no `remarkPlugins`
 * are passed anywhere, so CommonMark-only react-markdown parses a Markdown table
 * as paragraphs of literal `|` text and never emits a `<table>` at all. Adding
 * the plugin is an npm dependency change and out of scope here — flagged in the
 * report. The override is written now so the accessible structure lands with the
 * plugin rather than a release after it.
 */
const MARKDOWN_COMPONENTS: Components = {
  // `children` only — deliberately not spreading react-markdown's props. It
  // passes a `node` (the mdast node) that must never reach the DOM, and a GFM
  // `<table>` carries no attributes of its own (alignment lives on th/td).
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table style={{ display: "table" }}>{children}</table>
    </div>
  ),
};
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
            <Markdown components={MARKDOWN_COMPONENTS}>
              {data.content_md}
            </Markdown>
          </div>
        )}
      </div>
    </section>
  );
}
