/**
 * Builds the realtime connect URL. The socket authenticates via a `?token=`
 * query param (Phase 24), so the ticket (a short-lived bearer) is fetched from
 * the BFF in `lib/bff/client.ts`. Pure — no fetch, no DOM.
 */
export function wsUrl(token: string): string {
  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return `${base.replace(/^http/, "ws")}/ws?token=${encodeURIComponent(token)}`;
}
