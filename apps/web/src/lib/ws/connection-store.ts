"use client";

import { create } from "zustand";

/**
 * The always-visible connection status for the top-bar pill (TITAN-FRT-020).
 *
 *   - `connecting`  — first handshake, no socket yet
 *   - `live`        — socket open and subscribed
 *   - `reconnecting`— dropped, backing off to retry (amber pill)
 *   - `offline`     — reconnecting for > 60 s; a manual-retry banner shows and
 *                     the active screen falls back to 30 s polling (§5.3)
 */
export type ConnectionStatus =
  "connecting" | "live" | "reconnecting" | "offline";

interface ConnectionState {
  status: ConnectionStatus;
  /** True once degraded past the 60 s threshold — drives the polling fallback. */
  degraded: boolean;
  setStatus: (status: ConnectionStatus) => void;
}

export const useConnectionStore = create<ConnectionState>((set) => ({
  status: "connecting",
  degraded: false,
  setStatus: (status) => set({ status, degraded: status === "offline" }),
}));

/** Read the current connection status (for the pill and polling gates). */
export function useConnection(): {
  status: ConnectionStatus;
  degraded: boolean;
} {
  // Select primitives, never a fresh object: zustand v5 feeds the selector
  // result straight to useSyncExternalStore, so returning a new object each
  // render would make the snapshot unstable and loop forever.
  const status = useConnectionStore((s) => s.status);
  const degraded = useConnectionStore((s) => s.degraded);
  return { status, degraded };
}
