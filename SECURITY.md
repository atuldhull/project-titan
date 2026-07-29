# Security Policy

PROJECT TITAN takes the security of the platform and the code its agents generate
seriously. This document describes how to report a vulnerability, our response
commitments, and our disclosure and supported-versions policy (TITAN-SEC-121).

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.** Instead, report it
privately through one of:

- **GitHub Security Advisories** — the "Report a vulnerability" button under the
  repository's **Security** tab (preferred; creates a private advisory).
- **Email** — `security@titan.example` with the details below (PGP key published
  at `/.well-known/security.txt`).

Please include: a description and impact assessment, the affected version /
commit, reproduction steps or a proof-of-concept, and any suggested remediation.
Reports of issues in **agent-generated project code** should identify the
generating run (trace id) so we can locate the gate miss.

## Response SLAs

We acknowledge and triage on the following targets (Vol 8 §13.1 severity levels):

| Severity | Definition | Acknowledge | Target resolution |
|---|---|---|---|
| **SEV-1** | Active compromise or secret exposure (leaked key, sandbox escape, broken audit chain) | 24 h | Contain ≤ 1 h once confirmed |
| **SEV-2** | Exploitable weakness, no confirmed abuse (auth bypass, injection reaching the tool layer) | 2 business days | Mitigate ≤ 24 h |
| **SEV-3** | Control failure without direct exposure (scanner outage, redaction false-negative class) | 5 business days | Fix ≤ 7 d |

## Coordinated disclosure

We follow **90-day coordinated disclosure**. We will keep you updated on our
progress and coordinate a public disclosure date once a fix is available; we ask
that you give us up to 90 days before any public disclosure. We credit reporters
in the advisory unless you prefer to remain anonymous.

## Supported versions

Security fixes are provided for the **latest released minor version** and the one
immediately preceding it. Older versions receive fixes only for SEV-1 issues, at
our discretion. Users are expected to stay current with digest-pinned, signed
release images (TITAN-SEC-101).

## Our security posture

TITAN's controls are documented in `Project-TITAN-Docs/08-Volume-8-Security.md`
and traced, machine-readably, in [`security/compliance-map.yaml`](security/compliance-map.yaml)
(OWASP Top 10 2021 + OWASP LLM Top 10 2025 → requirement IDs → tests, enforced by
CI, TITAN-SEC-110). The CI pipeline blocks merges on detected secrets, high+ SAST
findings, and high/critical dependency or image vulnerabilities (TITAN-SEC-100),
and an agent-dependency gate (TITAN-SEC-104) blocks hallucinated, typosquatted,
unvetted, or vulnerable packages at the single caching-mirror enforcement point.
