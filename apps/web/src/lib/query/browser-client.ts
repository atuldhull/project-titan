"use client";

import { createTitanClient, type TitanClient } from "@titan/api-client";

/**
 * The browser-side typed client. It never talks to FastAPI directly — its base
 * URL is the same-origin BFF proxy (`/api/titan`), which re-attaches the bearer
 * from the httpOnly session cookie server-side. Tokens therefore never enter
 * client JS (TITAN-SEC-013). Because the proxy preserves the path verbatim, the
 * generated `/api/v1/...` operation paths are used unchanged.
 */
let client: TitanClient | undefined;

export function browserApi(): TitanClient {
  if (!client) {
    client = createTitanClient({ baseUrl: "/api/titan" });
  }
  return client;
}
