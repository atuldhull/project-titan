import type { Problem } from "@titan/api-client";

/** Media type for RFC 9457 problem documents — the TITAN error contract. */
export const PROBLEM_CONTENT_TYPE = {
  "content-type": "application/problem+json",
} as const;

/** Build a BFF-originated problem document (e.g. a 401 before FastAPI is hit). */
export function problem(
  status: number,
  title: string,
  detail?: string,
): Problem {
  return detail === undefined
    ? { type: "about:blank", title, status }
    : { type: "about:blank", title, status, detail };
}
