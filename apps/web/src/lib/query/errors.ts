"use client";

import { toast } from "sonner";

import { ApiError } from "@titan/api-client";

/**
 * Global mutation-error handler (TITAN-FRT-037, TITAN-FRT-015). A forged
 * mutation from an under-privileged session returns 403 at the server (the
 * authority) even when the UI failed to hide the control — here that surfaces a
 * permission toast. An expired session (401) prompts re-login.
 */
export function handleMutationError(error: unknown): void {
  if (!(error instanceof ApiError)) {
    return;
  }
  if (error.isForbidden) {
    toast.error("You don’t have permission to do that.");
  } else if (error.isUnauthorized) {
    toast.error("Your session expired — please sign in again.");
  }
}
