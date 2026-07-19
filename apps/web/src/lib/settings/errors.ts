import { ApiError } from "@titan/api-client";

/**
 * A message a human can act on, from whatever the mutation threw.
 *
 * The API answers RFC-7807 problem+json, so the useful sentence is in `detail`.
 * Two statuses get a rewrite because the raw text misleads:
 *
 * * **403** is TITAN-FRT-037's permission case. The UI hides controls a role
 *   cannot use, but hiding is never the control — the server re-checks every
 *   mutation, and a forged request lands here. It must read as "you may not",
 *   not as a failure of the thing you asked for.
 * * **404 on a project-scoped route** is a permissions answer in disguise: the
 *   no-IDOR rule (TITAN-SEC-032) returns 404 rather than 403 when the caller
 *   holds nothing in that project, so "not found" can mean "not yours".
 */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return (
        detailOf(error) ||
        "You don’t have permission to do that. Ask an owner or admin."
      );
    }
    if (error.status === 503) {
      return (
        detailOf(error) || "That service is not enabled on this deployment."
      );
    }
    return detailOf(error) || `Request failed (HTTP ${error.status}).`;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Something went wrong.";
}

function detailOf(error: ApiError): string {
  const problem = error.problem as { detail?: unknown; title?: unknown } | null;
  if (problem && typeof problem.detail === "string" && problem.detail) {
    return problem.detail;
  }
  if (problem && typeof problem.title === "string" && problem.title) {
    return problem.title;
  }
  return "";
}

/** True when the failure is specifically "you may not" (TITAN-FRT-037). */
export function isPermissionDenied(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}
