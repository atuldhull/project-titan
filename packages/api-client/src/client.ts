/**
 * Typed TITAN REST client (TITAN-FRT-014, TITAN-FRT-016).
 *
 * Wraps `openapi-fetch` over the generated `@titan/schemas` `paths`, so every
 * call is checked against the real OpenAPI contract and the whole surface moves
 * in lock-step with the backend (a drift gate keeps `openapi.json` honest).
 *
 * This module — together with the Next.js BFF route handlers — is the ONLY
 * place `fetch` is allowed (an ESLint rule enforces it). Two client shapes are
 * built from the same factory:
 *   - server-side (RSC + route handlers): `baseUrl` = FastAPI origin, with the
 *     access token from the httpOnly session cookie injected as a bearer;
 *   - browser (React Query hooks): `baseUrl` = the same-origin `/api/titan`
 *     proxy, which re-attaches the bearer server-side — tokens never reach JS
 *     (TITAN-SEC-013).
 */

import createClient, { type Client } from "openapi-fetch";
import type { components, paths } from "@titan/schemas";

/** RFC 9457 problem document — the shape of every TITAN error body. */
export type Problem = components["schemas"]["Problem"];

export type TitanClient = Client<paths>;

export interface TitanClientOptions {
  /** Origin (server) or same-origin proxy prefix (browser), e.g. `/api/titan`. */
  readonly baseUrl: string;
  /** Injected fetch — a custom impl in tests, or the platform default. */
  readonly fetch?: typeof fetch;
  /** Static headers merged into every request (e.g. a server-side bearer). */
  readonly headers?: Record<string, string>;
}

export function createTitanClient(options: TitanClientOptions): TitanClient {
  return createClient<paths>({
    baseUrl: options.baseUrl,
    fetch: options.fetch,
    headers: options.headers,
  });
}

/**
 * Error thrown by `unwrap` for any non-2xx response. Carries the parsed RFC 9457
 * `Problem` body when present; `status` is the definitive signal (403 → the UI
 * shows a permission toast; 401 → the session is re-established).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, message?: string) {
    super(
      message ??
        problem?.detail ??
        problem?.title ??
        `TITAN API request failed with status ${status}`,
    );
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** 404 with no visibility is how the backend hides existence (no-IDOR). */
  get isNotFound(): boolean {
    return this.status === 404;
  }
}

function isProblem(value: unknown): value is Problem {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "title" in value
  );
}

/**
 * Narrow an openapi-fetch `{ data, error, response }` result to its success
 * payload, throwing a typed {@link ApiError} otherwise. React Query query/
 * mutation functions call this so failures flow through the normal error path
 * (rollback + retry toast). `T` is inferred as the operation's success body.
 */
export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): NonNullable<T> {
  if (result.data !== undefined && result.data !== null) {
    return result.data as NonNullable<T>;
  }
  const status = result.response?.status ?? 0;
  const problem = isProblem(result.error) ? result.error : null;
  throw new ApiError(status, problem);
}
