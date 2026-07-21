# ADR-SEC-03 (impl) — the secrets vault, field encryption, and signed envelopes

Phase 68. Sources: Vol 8 §6.1–6.3 (TITAN-SEC-050/051/052/053), ADR-SEC-03, §3.5
(TITAN-SEC-020), §7 (TITAN-SEC-063). This record reconciles three places where
the spec's own text does not, and pins the decisions the code encodes.

## 1. `titan.vault` is a leaf, not a domain

The vault is imported by the event bus (envelope signing), by `titan.identity`
(MFA secret encryption), by `titan.tools` (sandbox injection), by `titan.plugins`
(config cipher + secret resolution), and by its own router. A domain module
could not be that shared thing without breaking the module-independence
contracts. So `titan.vault` is an infrastructure leaf: its crypto submodules
(`cipher`, `signing`, `age`, `provider`, `drivers`) import only stdlib +
`cryptography` + `titan.db`/`titan.settings`, never a domain. The bus never
imports the vault at all — the signer and the audit hook are **injected** at the
composition root (`main`/`worker`), which is the only way `titan.events` stays a
broker-swappable leaf while still enforcing SEC-020.

The Phase-52 `titan.plugins.secrets` names (`ConfigCipher`, `EnvSecretsProvider`,
`verify_hmac_sha256`, …) now re-export from here. One home, no fork.

## 2. SEC-050 "no secrets in Postgres" vs SEC-063 "encrypt these columns"

SEC-050 says secrets live "never in code, git, plain config, Redis, or Postgres
application tables." SEC-063 says to store OAuth tokens and webhook signing
secrets as AES-256-GCM columns. Read literally, these collide. The reconciliation
the code encodes: **SEC-050 forbids _plaintext_ secret material in Postgres;
vault-keyed ciphertext columns are the sanctioned exception SEC-063 carves out.**
A secret VALUE in the clear never touches an application table. The vault's
plaintext lives only in one process's memory (the age file decrypted at boot);
Postgres holds secret _metadata_ (`secret_metadata`) and _ciphertext_ (a field
cipher blob), never a value.

### What SEC-063 actually encrypts here

SEC-063 names "stored OAuth tokens, webhook signing secrets." In this
architecture **those columns do not exist**: connector tokens and webhook
secrets are resolved by _reference_ from the vault/env (SEC-050), never persisted
(scouted and confirmed — see `plugins/manifest.py` `permissions.secrets`, all
name-only). The one plaintext secret column that DID exist is
`mfa_credentials.secret` (the TOTP shared secret; Phase 67 left it plaintext with
a comment deferring encryption "to Phase 68"). So the SEC-063 _mechanism_ (a
generalized `FieldCipher` with key-version tags, keyed from the vault) is applied
to the real highest-sensitivity plaintext column that exists — the MFA secret —
and is ready for any future stored-token column. Decryption tolerates a legacy
plaintext row (a base32 secret never matches the `v<n>.<b64>.<b64>` blob shape),
so an existing enrollment keeps working and re-enrolling re-writes it encrypted.

## 3. SEC-020 envelope fields vs the real wire envelope (TITAN-BCK-010)

SEC-020 specifies `{msg_id, sender_principal, task_id, ts, payload_sha256, hmac}`.
The envelope actually on the wire (BCK-010, shared by every producer/consumer and
the outbox) is `{event_id, topic, occurred_at, actor, payload, trace_id}`. The
field names do not match, and the specs never reconcile them. Decision: **SEC-020
is a signature WRAPPER over the BCK-010 envelope, not a rename.** The HMAC is
computed over the entire serialized BCK-010 envelope and rides in a sibling `sig`
stream field. The SEC-020 conceptual fields map onto data the signature already
covers: `msg_id`→`event_id`, `sender_principal`→`actor`, `ts`→`occurred_at`,
`payload`→`payload`; `payload_sha256` is subsumed because the HMAC authenticates
the whole serialization, which is strictly stronger than signing a digest of the
payload alone. Renaming the wire contract would have touched every producer,
consumer, the outbox, and the DLQ for no security gain.

**Drop, don't wedge.** A missing/bad/unparseable envelope is ACKed (removed from
the pending list so it can never redeliver or wedge the reaper — the exact
pre-existing failure mode where an unguarded `from_wire` before the poison check
would loop forever) and reported to an audit hook (`secret_audit`,
`envelope.rejected`). It is deliberately NOT dead-lettered (a forged-frame flood
would grow the DLQ unbounded) and NOT marked-seen (a forged, attacker-chosen
`event_id` marked seen would poison the dedup ledger against a future legitimate
event with that id).

## 3a. Fail-closed on empty crypto keys outside local

Both `bus_hmac_key` and `field_encryption_key` derive a *usable but insecure*
fallback when empty — a source-visible fixed HMAC key, and an ephemeral
per-process AES key. A warning is not enough: a forgotten `TITAN_BUS_HMAC_KEY`
in production would sign and verify every SEC-020 envelope with a key printed in
this repo (trivial forgery — the exact fail-open the control exists to prevent),
and a forgotten `TITAN_FIELD_ENCRYPTION_KEY` would lock every MFA-enrolled user
out after any restart or on a second worker (the ephemeral key differs per
process). So `Settings._require_crypto_keys_outside_local` **refuses to boot**
when `environment != "local"` and either key is empty — the same fail-closed
stance as the `trusted_proxies` validator and `seed.py`'s refusal to seed a
default admin password outside local. The fixed-dev-key / ephemeral-key
conveniences survive only in local. (`vault_age_key` is deliberately NOT
required — an env-backed vault is a supported prod mode per SEC-050, and the
write API answers 503 rather than failing open.) Defense in depth: `verify_totp`
also catches a decrypt failure and falls through to the recovery-code path
rather than 500-ing, so an undecryptable secret never blocks recovery login.

Key rotation is real (SEC-063 acceptance): `FieldCipher` decrypts by trying the
current key then `field_encryption_key_previous`, so rotating the key reads old
rows under the old key while new writes use the new — the same verify-key window
`HmacEnvelopeSigner` uses for the bus key.

## 4. SOPS+age default driver

ADR-SEC-03 wants a "SOPS+age encrypted YAML" default with zero infrastructure.
No `age`/`sops` binary is assumed present (SEC-002: plain Compose, free
components only), and `cryptography` already ships every age primitive. So the
default driver is a **self-contained, interoperable age v1 (X25519)
implementation** (`titan.vault.age`): the vault file is a real `age` file an
operator can decrypt with the `age` CLI, unlocked at boot into an in-memory map,
written atomically (re-encrypt) on create/rotate/delete. Vault/OpenBao slots in
behind the same `SecretsStore` interface, configuration-only. The age identity
(`TITAN_VAULT_AGE_KEY`) is a class-1 platform secret — never committed; the
ciphertext file is git-safe.

## 5. Write-only, scoped, audited

- **Write-only (SEC-051).** The API creates/rotates/deletes; it never returns a
  value. `GET /secrets/{name}/value` exists only to answer **405** so the
  contract is discoverable; metadata reads require the carved-out `secret:read`.
  RBAC: `secret` is in `SENSITIVE_RESOURCES` (singular, matching the resource
  parsed from `secret:write`), so `*:read` never reaches it — grants are explicit
  (Owner/Admin/Maintainer, §4.2). Agents (`operator`) hold neither scope.
- **Scoped injection (SEC-053).** A sandbox gets only the secrets its task's
  granted tools declare (`ToolSpec.required_secrets`), as `/run/secrets` tmpfs
  files (0400, sandbox-uid, RAM, freed on stop) — never env vars, which
  `terminal.run` lets the model override. No builtin tool declares any today, so
  every current task (a docs task included) injects zero integration secrets; the
  LLM gateway alone holds provider keys.
- **Audited (§10.2, SEC-092).** create/rotate/delete/inject and envelope
  rejections write `secret_audit` rows carrying names/digests, never values.
