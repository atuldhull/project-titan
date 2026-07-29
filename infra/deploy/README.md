# Deploy manifests (digest-pinned only)

Production/staging deploy manifests live here. Per **TITAN-OPS-005** (Vol 9 §3.1),
every TITAN application image (`ghcr.io/titan-platform/*`) referenced in a manifest
**MUST** be pinned to an immutable `@sha256:` digest — never `:latest` and never a
floating `:sha-…`/`:vX.Y.Z` tag. The digests come from the cosign-signed push in
`.github/workflows/main.yml`; `release.yml` retags the same digest for production.

`apps/api/scripts/check_deploy_digests.py` (Makefile `check-deploy-digests`, run in
CI) enforces this on every manifest in this directory. In-cluster, a Kyverno/
Gatekeeper policy additionally rejects unsigned images and floating tags.

The Kubernetes overlays themselves land with the staging/production infra phase
(Phase 78); this directory + the CI gate exist now so the rule is enforced from the
first manifest committed.
