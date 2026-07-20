/**
 * PKCE (RFC 7636) for the BFF's server-side login (Phase 67; TITAN-SEC-010).
 *
 * The BFF performs the Authorization Code + PKCE flow ITSELF, in one request:
 * it generates a verifier, sends the S256 challenge to `/oauth/authorize`,
 * receives a code, and redeems it at `/oauth/token` with the verifier — all
 * server-side, so the verifier never reaches the browser and there is no
 * cross-site callback to protect. The "redirect" is degenerate (same process),
 * but the security properties that matter are intact: the code is single-use
 * and PKCE-bound, and a stolen code is useless without the verifier the BFF
 * never emitted.
 *
 * Uses Web Crypto, which is a global in the Next server runtime — no Node
 * `crypto` import, so this stays edge-compatible.
 */

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export interface Pkce {
  readonly verifier: string;
  readonly challenge: string;
}

/**
 * A fresh verifier (96 base64url chars, comfortably inside the RFC's 43–128)
 * and its S256 challenge.
 */
export async function createPkce(): Promise<Pkce> {
  const random = new Uint8Array(72);
  crypto.getRandomValues(random);
  const verifier = base64UrlEncode(random);
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  const challenge = base64UrlEncode(new Uint8Array(digest));
  return { verifier, challenge };
}

/**
 * The nominal redirect_uri bound to the code. The BFF redeems the code itself,
 * so this value never drives a browser navigation — it only has to match
 * exactly between the authorize and token calls, which the embedded IdP checks
 * (the exact-match binding that stops an intercepted code being redeemed
 * elsewhere).
 */
export const BFF_REDIRECT_URI = "titan-bff:/auth/callback";
