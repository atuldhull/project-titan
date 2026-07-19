import type { Metadata } from "next";

import { ApiKeysScreen } from "@/components/settings/api-keys-screen";

export const metadata: Metadata = { title: "API Keys" };

/**
 * Settings → API Keys (Vol 4 §6.13).
 *
 * No RSC prefetch: creation returns a secret that must never be rendered on the
 * server or embedded in the flight payload, and the list is short enough that a
 * client fetch costs nothing worth optimising.
 */
export default function Page() {
  return <ApiKeysScreen />;
}
