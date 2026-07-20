"use client";

import { CONNECTION_PRESENTATION } from "@/lib/ws/connection-presentation";
import { useConnection } from "@/lib/ws/connection-store";
import { cn } from "@/lib/utils";

/**
 * The always-visible realtime connection indicator in the top bar
 * (TITAN-FRT-020): live / reconnecting (amber) / offline. Colors are semantic
 * tokens only (TITAN-FRT-010).
 *
 * The label/dot map now lives in lib/ws/connection-presentation.ts: the Overview
 * activity feed shows the same status and has to word it identically.
 *
 * This pill is the ONE announcer of connection changes — it is mounted in the
 * top bar on every screen, so any other consumer must render the status
 * silently or the same transition is spoken twice.
 */
export function ConnectionPill() {
  const { status } = useConnection();
  const { label, dot, pulse } = CONNECTION_PRESENTATION[status];
  return (
    <span
      className="hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs text-muted-foreground sm:flex"
      role="status"
      aria-live="polite"
      title={`Realtime connection: ${label}`}
    >
      <span
        aria-hidden
        className={cn("size-1.5 rounded-full", dot, pulse && "animate-pulse")}
      />
      {label}
    </span>
  );
}
