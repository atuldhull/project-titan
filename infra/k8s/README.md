# TITAN on Kubernetes (Vol 9 §4, §5, §8.1)

Kustomize base + staging/production overlays deploying the §4.1 topology, plus cluster-scoped
Kyverno admission policies. The **same container images as Compose**, reconfigured by env only
(ADR-008) — never a forked config.

```
infra/k8s/
  base/         namespace-agnostic manifests (workloads, datastores, ingress, PDBs, netpol)
  overlays/
    staging/    namespace titan-staging, trimmed replicas, TITAN_ENVIRONMENT=staging
    production/ namespace titan-prod, ResourceQuota + LimitRange, digest pinning at deploy
  policies/     Kyverno ClusterPolicies (cluster-scoped; applied separately)
  kind-config.yaml   3-worker kind cluster for local HA validation
```

## Render & validate

```bash
kubectl kustomize infra/k8s/overlays/production        # render
make k8s-validate                                       # kustomize build | kubeconform (all layers)
make check-k8s                                          # security posture (no :latest; datastores edge-unreachable)
```

CI runs both: `check-k8s` (pure-Python, every PR) and the `k8s-manifests` job (kubeconform over
base + both overlays + policies). CNPG and Kyverno CRDs are skipped by kubeconform by design.

## Topology highlights

- **Workloads (§4.2):** Deployments for frontend/api/orchestrator/worker/llm-gateway; StatefulSets
  for Redis and Qdrant; Postgres via the **CloudNativePG** operator (`Cluster`, 1 primary + 1
  replica, real streaming replication + auto-failover). Datastores are ClusterIP/headless —
  **never** an Ingress (OPS-007), enforced statically by `check_k8s.py`.
- **Probes (OPS-008):** api/orchestrator/llm-gateway readiness is `/readyz` (503 when Postgres/
  Redis/Qdrant are down → a bad DSN **stalls the rollout**, never an outage). The worker has no
  HTTP server, so its readiness is `titan health` (same dependency check).
- **HA (§8.1) — Postgres only, honestly:** Postgres HA is fully real (CNPG primary+replica,
  automatic failover). PodDisruptionBudgets + pod anti-affinity + zone topology spread apply to
  every tier. **The orchestrator/worker run 1 replica and Redis/Qdrant run single-instance** —
  see "HA gaps" below; these are NOT claimed as delivered because they need app-level features,
  not manifests. Recovery for the single-instance tiers is via K8s reschedule.
- **Ingress:** ingress-nginx for `app.titan.dev` / `api.titan.dev`, cert-manager TLS; the api
  Ingress carries extended proxy timeouts so `/ws` WebSockets aren't cut at 60s.
- **NetworkPolicies:** default-deny ingress for every pod + default-deny egress for the app tiers;
  only the §4.1 arrows are permitted; only the llm-gateway may egress to the internet (TCP 443).
  Datastores/migrate/CNPG keep egress open so replication, migrations, and operator control work.
- **Storage:** PVCs use the cluster's **default** StorageClass so the stack comes up on kind
  (local-path) out of the box; production points its default class at SSD (OPS-007) or patches it.

## Image digest pinning (OPS-005)

Base/overlays reference **pinned version tags** (never `:latest`, enforced by `check_k8s.py` +
Kyverno). The intended deploy flow pins them to immutable digests: `release.yml` already resolves
(`crane digest`) and **cosign-signs** the digests when it pushes; the deploy step then runs
`kustomize edit set image ghcr.io/titan-platform/titan-api@sha256:<digest>` per image. **Honest
status:** that deploy/apply step in `release.yml` is currently a stub (it lands with the cluster
infra), so digest pinning is wired at the signing end but not yet applied at deploy. Digests are
per-build and resolved at publish time — never fabricated/committed here. Kyverno's
`verify-titan-image-signatures` rejects any unsigned image at admission regardless.

## Local HA cluster (the DoD's kind test)

```bash
kind create cluster --config infra/k8s/kind-config.yaml     # 3 workers across faux zones
# install operators: CloudNativePG, ingress-nginx, cert-manager, Kyverno (see their docs)
kubectl apply -k infra/k8s/policies                          # admission policies
kubectl apply -k infra/k8s/overlays/staging                 # the stack
```

## Verified vs. CI/cluster-only

- **Verified locally (Windows box):** kustomize build + kubeconform on base + both overlays +
  policies; the `check_k8s.py` security gate (with re-break).
- **CI/cluster-only (no kind/kyverno/operators on the dev box):** healthy rollout on kind, the
  pod-kill recovery drill (CNPG primary / Redis master / orchestrator leader), and the Kyverno
  unsigned-image rejection. These run where a cluster exists; the manifests are authored
  correct-by-construction and schema-validated here.
- **Known gaps (honest — need app-level work, not manifests; tracked for follow-up):**
  - *Leader election / active-passive orchestrator (§8.1):* the app has no session-level leader
    lock, and the scheduler/singleton loops (domain-stream fan-out preserving per-project order,
    periodic reconcilers) run inside `titan.worker` under a hardcoded consumer name. So
    orchestrator + worker run **1 replica**; scaling the worker to §4.3's 2–50 (Phase 80 KEDA)
    requires first leader-electing / separating those singleton loops. `titan-orchestrator` is
    currently the `titan-core` image (its Dockerfile notes the standalone-scheduler extraction).
  - *Redis primary/replica + Sentinel failover (§8.1):* needs `replicaof` wiring + a Sentinel-aware
    client (`titan.clients` uses a plain `Redis.from_url`). Redis runs **single-instance** today.
  - *Qdrant replication factor 2 (§8.1):* needs Qdrant cluster mode + a cluster-aware client;
    Qdrant runs **single-instance** today.
  - *llm-gateway image:* no dedicated `titan-llm-gateway` image — the gateway is an in-process
    library, so its Deployment runs `titan-api` with `TITAN_ROLE=llm-gateway` per §4.2.
  - *CNPG WAL archiving (§8.2):* added via `Cluster.spec.backup` once the object store exists.
