"use client";

/**
 * The "· lines skipped [load] ·" divider (§6.7).
 *
 * §6.7 words this "reconnected · N lines skipped". Both halves are adjusted to
 * what is true here:
 *
 * - not "reconnected" — the tail polls the history endpoint rather than
 *   streaming (there is no `log.appended` producer, and the app socket would
 *   shed a connection at §6.7's own 1,000 lines/s), so a gap is not a dropped
 *   socket. It is the viewer choosing to jump forward rather than replay a
 *   backlog after a long absence.
 * - not "N lines" — N is not knowable without asking Loki to COUNT the window,
 *   a second query over the same range. The marker states the time span it
 *   skipped, which is exact and known, and shows a real count once a fill has
 *   actually returned lines.
 */
import { AlertTriangle, Download, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { gapDurationMs, type Gap } from "@/lib/logs/gaps";

export function formatSpan(ms: number): string {
  if (ms < 1_000) return `${ms}ms`;
  const seconds = Math.round(ms / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function gapLabel(gap: Gap): string {
  const span = formatSpan(gapDurationMs(gap));
  if (gap.filled > 0) {
    // Once a fill has run the count is real, so say it — and say that there is
    // more only when the backend actually offered another page.
    return `${gap.filled.toLocaleString()} lines loaded · more remaining in ${span}`;
  }
  return `${span} of logs not shown`;
}

export function GapMarker({
  gap,
  onLoad,
}: {
  readonly gap: Gap;
  readonly onLoad: (gap: Gap) => void;
}) {
  return (
    <div
      className="flex items-center gap-2 px-2 py-1"
      role="separator"
      aria-label={gapLabel(gap)}
    >
      <span className="h-px flex-1 bg-border" />
      <span className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
        {gap.status === "error" && (
          <AlertTriangle className="h-3 w-3 text-destructive" aria-hidden />
        )}
        {gap.status === "error" ? (
          <span className="text-destructive">Couldn’t load: {gap.error}</span>
        ) : (
          gapLabel(gap)
        )}
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-5 gap-1 px-2 text-[11px]"
          disabled={gap.status === "loading"}
          onClick={() => onLoad(gap)}
        >
          {gap.status === "loading" ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : (
            <Download className="h-3 w-3" aria-hidden />
          )}
          {gap.status === "error" ? "Retry" : "Load"}
        </Button>
      </span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}
