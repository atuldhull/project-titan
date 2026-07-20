# Authentication grants: why a local password mode is not a password grant

Phase 67. Sources of truth: Vol 8 §3.1–§3.2 (TITAN-SEC-010/013/015/017,
ADR-SEC-01), Vol 3 §9.2 (TITAN-BCK-020).

## The contradiction

Two MUST-level requirements appear to demand opposite things, and neither text
resolves it.

> **TITAN-SEC-010 (MUST):** Login uses OAuth2 Authorization Code + PKCE;
> implicit and **password grants disabled**. *Acceptance:* implicit-flow
> attempt returns error.

> **TITAN-BCK-020 (MUST)** — AuthN SHALL be OAuth2/OIDC (authorization-code +
> PKCE) against a pluggable IdP … **plus a `local` password mode for
> laptop-only use.**

Read literally, SEC-010 forbids the thing BCK-020 requires. Neither requirement
mentions the other. Vol 8 §3.1's prose describes the embedded IdP as "FastAPI +
authlib, username/password + TOTP" sitting *behind* an Authorization Code +
PKCE flow, which points at the resolution below — but it never uses the words
"password grant" or "local mode", so the reconciliation is not written down
anywhere in the specification. This ADR is where it gets written down.

## Decision

**"Password grant" and "password mode" name different things, and only one of
them is a grant.**

- **The password grant** is OAuth2's Resource Owner Password Credentials grant
  (`grant_type=password`, RFC 6749 §4.3): a *client* collects the user's
  password and posts it to the token endpoint. It is deprecated in OAuth 2.1
  and is what SEC-010 disables. It stays disabled, at the endpoint, returning
  `unsupported_grant_type`.

- **Local password mode** is a deployment mode of the *authorization server*.
  The embedded IdP owns the credential, presents its own first-party login
  form, and on success completes the ordinary Authorization Code + PKCE flow.
  No client ever sees the password. That is what BCK-020 means by "laptop-only
  use": no external IdP to federate to, so the embedded one authenticates
  directly.

The distinction that carries the whole argument: **who the password is
presented to.** A first-party form posts it to the authorization server that
issued and stores the credential. The password grant relays it through a
client. The first is the normal, recommended shape for an authorization server
with local accounts; the second is what every modern OAuth guidance document
tells you to stop doing, for reasons — credential relay, no MFA step-up point,
no redirect-based interruption for consent or policy.

So both requirements hold simultaneously, with no exception carved into either:

| | Password grant (SEC-010 disables) | Local password mode (BCK-020 requires) |
|---|---|---|
| Where the password is typed | any client's own UI | the IdP's `/login` form |
| Who receives it | token endpoint, as `grant_type=password` | authorization endpoint session |
| What the client receives | tokens, directly | an authorization code, via redirect |
| PKCE | not applicable | required |
| Room for MFA / policy step-up | none | the redirect interruption |

`AUTH_MODE` therefore selects **which IdP authenticates the user**, never which
grant is used. `local` uses the embedded IdP's own form; `oidc` federates to an
external provider. Both terminate in the same Authorization Code + PKCE
exchange, and TITAN mints its own RS256 tokens afterwards either way — the
Phase-8 issuer is not replaced, extended, or duplicated (ADR-SEC-01).

## What this repo was actually doing

Recording this plainly, because the ADR would otherwise read as a
formality about a hypothetical.

**The shipped login used the exact grant SEC-010 forbids.** The Phase-57 BFF
route `POST /api/auth/login` called FastAPI `/api/v1/auth/token` with
`grant_type: "password"`, relaying the user's password from the browser through
the Next.js server to the token endpoint. That is the Resource Owner Password
Credentials grant, textbook. It passed every gate because nothing tested
SEC-010 — its acceptance criterion is "implicit-flow attempt returns error",
and no implicit flow existed to attempt.

Two further gaps in the same area, both verified rather than assumed:

- **`AUTH_MODE` was dead configuration.** `settings.py` declared
  `Literal["local", "oidc"]` and nothing ever read it; the router called
  `login_local` unconditionally. Setting it to `oidc` changed nothing.
- **BCK-020's acceptance criterion is unreachable.** It asks for a "full login
  round-trip against dockerized Keycloak in the integration suite". There is no
  Keycloak service in `docker-compose.yml` — only a header comment saying it
  "joins the core in later phases" — and the `infra/keycloak` realm directory
  that `infra/README.md` references does not exist.

## Consequences

- The token endpoint rejects `grant_type=password` and `grant_type=implicit`
  (and the authorization endpoint rejects `response_type=token`) with OAuth
  errors, rather than merely not implementing them. A grant that is absent
  because nobody wrote it is indistinguishable from one that is absent because
  it is forbidden; only the second survives someone adding a convenience
  endpoint later.
- Local mode costs a redirect. `/login` is a real page on the IdP, not a form
  the SPA posts in the background, and the BFF completes a code exchange rather
  than receiving tokens directly. This is the price of having one code path for
  both modes, and of having a place to put an MFA challenge (TITAN-SEC-015).
- Federation stays config-only. Because both modes end at the same exchange,
  pointing `AUTH_MODE=oidc` at Keycloak, Google, GitHub or Okta changes an
  issuer URL and a client registration, not any application code.
- Passwords remain Argon2id (TITAN-SEC-017) in local mode, and simply do not
  exist in `oidc` mode — `local_credentials` is a separate table from `users`
  precisely so a federated user never carries a hash.
