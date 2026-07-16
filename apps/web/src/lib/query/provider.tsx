"use client";

import { QueryClientProvider } from "@tanstack/react-query";

import { getQueryClient } from "./client";

/**
 * Mounts the browser React Query client (Vol 4 §4.1). Placed inside the
 * auth-gated dashboard group only — public routes ship no query runtime.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const client = getQueryClient();
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
