"use client";

import type { Session } from "@/lib/auth/types";

/**
 * Browser → BFF calls. These reach the same-origin Next route handlers under
 * `app/api/*` (login, logout, session, WS ticket) — infrastructure the httpOnly
 * cookie rides on, NOT server-state (which flows through React Query + the typed
 * client). This directory is the one place besides route handlers where `fetch`
 * is allowed (scoped in eslint.config.mjs, TITAN-FRT-014).
 */

async function postJson(url: string, body?: unknown): Promise<Response> {
  return fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers:
      body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export interface LoginResult {
  ok: boolean;
  session: Session | null;
  error?: string;
  /**
   * Set when the password was correct but a second factor is needed
   * (TITAN-SEC-015). "code" → the account has MFA and must supply a TOTP or
   * recovery code; "enroll" → a privileged account under org policy must enroll
   * MFA before it can sign in. The form branches on this instead of showing the
   * generic error.
   */
  mfa?: "code" | "enroll";
}

export async function login(
  email: string,
  password: string,
  totpCode?: string,
): Promise<LoginResult> {
  const response = await postJson("/api/auth/login", {
    email,
    password,
    ...(totpCode ? { totp_code: totpCode } : {}),
  });
  if (!response.ok) {
    let error = "Sign in failed";
    let type = "";
    try {
      const body = (await response.json()) as {
        detail?: string;
        title?: string;
        type?: string;
      };
      error = body.detail ?? body.title ?? error;
      type = body.type ?? "";
    } catch {
      // keep the default message
    }
    // The problem `type` URI is how the BFF passed the MFA signal through.
    if (type.endsWith("/mfa-required")) {
      return { ok: false, session: null, error, mfa: "code" };
    }
    if (type.endsWith("/mfa-enrollment-required")) {
      return { ok: false, session: null, error, mfa: "enroll" };
    }
    return { ok: false, session: null, error };
  }
  const data = (await response.json()) as { session: Session | null };
  return { ok: true, session: data.session };
}

export async function logout(): Promise<void> {
  await postJson("/api/auth/logout");
}

export async function fetchSession(): Promise<Session | null> {
  try {
    const response = await fetch("/api/auth/session", {
      credentials: "same-origin",
    });
    if (!response.ok) {
      return null;
    }
    const data = (await response.json()) as { session: Session | null };
    return data.session;
  } catch {
    return null;
  }
}

export async function fetchWsTicket(): Promise<string | null> {
  try {
    const response = await fetch("/api/ws-ticket", {
      credentials: "same-origin",
    });
    if (!response.ok) {
      return null;
    }
    const data = (await response.json()) as { token?: unknown };
    return typeof data.token === "string" ? data.token : null;
  } catch {
    return null;
  }
}
