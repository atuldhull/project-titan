import { NextResponse } from "next/server";

import { getSession } from "@/lib/auth/session";

/**
 * Returns the current session (decoded from the httpOnly cookie) or
 * `{ session: null }`. Used by the client to re-sync auth state across tabs and
 * after a refresh — the tokens themselves are never exposed.
 */
export async function GET(): Promise<NextResponse> {
  const session = await getSession();
  return NextResponse.json({ session });
}
