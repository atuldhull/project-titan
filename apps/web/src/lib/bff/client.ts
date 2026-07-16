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
}

export async function login(
  email: string,
  password: string,
): Promise<LoginResult> {
  const response = await postJson("/api/auth/login", { email, password });
  if (!response.ok) {
    let error = "Sign in failed";
    try {
      const body = (await response.json()) as {
        detail?: string;
        title?: string;
      };
      error = body.detail ?? body.title ?? error;
    } catch {
      // keep the default message
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
