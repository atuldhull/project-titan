"use client";

import {
  useConnection,
  type ConnectionStatus,
} from "@/lib/ws/connection-store";
import { cn } from "@/lib/utils";

/**
 * The always-visible realtime connection indicator in the top bar
 * (TITAN-FRT-020): live / reconnecting (amber) / offline. Colors are semantic
 * tokens only (TITAN-FRT-010).
 */
const PRESENTATION: Record<
  ConnectionStatus,
  { label: string; dot: string; pulse: boolean }
> = {
  connecting: { label: "Connecting…", dot: "bg-muted-foreground", pulse: true },
  live: { label: "Live", dot: "bg-success", pulse: false },
  reconnecting: { label: "Reconnecting…", dot: "bg-warning", pulse: true },
  offline: { label: "Offline", dot: "bg-destructive", pulse: false },
};

export function ConnectionPill() {
  const { status } = useConnection();
  const { label, dot, pulse } = PRESENTATION[status];
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
