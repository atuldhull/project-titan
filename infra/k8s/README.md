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
make check-autoscaling                                  # HPA/KEDA consistency (real metrics; bounds; §4.3)
make check-bluegreen                                    # blue/green topology (colors/Services/autoscalers, §5.1)
make test-bluegreen-scripts                             # release/rollback/slo-watch behaviour (fake kubectl)
```

CI runs all of these (pure-Python/bash, every PR) plus the `k8s-manifests` job (kubeconform over
base + both overlays + policies). CNPG and Kyverno CRDs are skipped by kubeconform by design.

## Topology highlights

- **Workloads (§4.2):** Deployments for frontend/api/orchestrator/worker/llm-gateway; StatefulSets
  for Redis and Qdrant; Postgres via the **CloudNativePG** operator (`Cluster`, 1 primary + 1
  replica, real streaming replication + auto-failover). Datastores are ClusterIP/headless —
  **never** an Ingress (OPS-007), enforced statically by `check_k8s.py`.
- **Probes (OPS-008):** api/orchestrator/llm-gateway readiness is `/readyz` (503 when Postgres/
  Redis/Qdrant are down → a bad DSN **stalls the rollout**, never an outage). The worker has no
  HTTP server, so its readiness is `titan health` (same dependency check).
- **HA (§8.1) — Postgres + worker real; the rest honest:** Postgres HA is fully real (CNPG
  primary+replica, automatic failover). The **worker now runs 2+ replicas** — its singleton loops
  are Postgres-advisory-lock leader-elected (`titan.runtime.leader`), so >1 replica is safe and
  KEDA scales it 2–50 on queue depth (§4.3). PodDisruptionBudgets + pod anti-affinity + zone
  topology spread apply to every tier. **The orchestrator still runs 1 replica and Redis/Qdrant
  run single-instance** — see "HA gaps" below; these need app-level features, not manifests.
  Recovery for the single-instance tiers is via K8s reschedule.
- **Autoscaling (§4.3, OPS-009):** HPAs on frontend (2–6), api (3–12, CPU + real
  `titan_http_requests_in_flight`), llm-gateway (2–8, CPU); a KEDA ScaledObject scales the worker
  2–50 on Redis-Streams consumer-group lag. Consistency is gated by `check_autoscaling.py`.
- **Blue/green (§5.1, OPS-010/011):** every stateless tier is deployed per COLOR (`<svc>-blue`/
  `<svc>-green`); the stable Service selects the active color, per-color preview Services front
  idle-color smoke tests, and `ops/scripts/release.sh` renders the idle color → smokes → flips the
  Service selector atomically → watches SLO 30 min → retires the old color. `slo-watch.sh`
  auto-rolls-back (via `rollback.sh`) on a confirmed fast-burn; `rollback.sh <service|all>` is the
  manual path. The git-canonical active color is **blue**; green is script-managed (never committed
  as an idle-color autoscaler, which would re-inflate it on `apply -k`). Gated by `check_bluegreen.py`.
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
- **Resolved since first authored:** *Worker leader election / horizontal scaling (§8.1, §4.3)* —
  the worker's singleton loops (domain-stream fan-out preserving per-project order, periodic
  reconcilers) are now Postgres-advisory-lock leader-elected (`titan.runtime.leader`), and the
  scalable consumer-group loops run per-replica with a unique consumer name. The worker runs
  **2 replicas** and KEDA scales it 2–50. This gap is closed.
- **Known gaps (honest — need app-level work, not manifests; tracked for follow-up):**
  - *Active-passive orchestrator (§8.1):* the orchestrator PROCESS has no session-level leader
    lock of its own — the singleton scheduling loops live in `titan.worker` (now leader-elected),
    so the orchestrator Deployment still runs **1 replica** (K8s reschedules on failure).
    `titan-orchestrator` is currently the `titan-core` image (its Dockerfile notes the
    standalone-scheduler extraction); a true active/passive orchestrator awaits that split.
  - *Redis primary/replica + Sentinel failover (§8.1):* needs `replicaof` wiring + a Sentinel-aware
    client (`titan.clients` uses a plain `Redis.from_url`). Redis runs **single-instance** today.
  - *Qdrant replication factor 2 (§8.1):* needs Qdrant cluster mode + a cluster-aware client;
    Qdrant runs **single-instance** today.
  - *llm-gateway image + egress choke point (§6.1/OPS-014):* no dedicated `titan-llm-gateway`
    image — the gateway is an in-process library, so the Deployment runs `titan-api`, and
    `TITAN_ROLE=llm-gateway` is **not yet read** by the app (the pod is a full api, not a scoped
    gateway). Because the gateway runs in-process in the worker/api pods, the NetworkPolicy that
    grants provider egress ONLY to `component=llm-gateway` (per OPS-014) would **block the
    worker's own LLM/embedding calls** if enabled as-is. Fix before prod: extract the gateway to
    a real service (route all LLM calls through it) OR grant the in-process-gateway tiers provider
    egress. Flagged in `networkpolicy.yaml`.
  - *Blue/green worker consumer group (§5.1):* both worker colors share the ONE `titan-runs-execute`
    group (a code constant, not per-color), so during a release both colors co-consume the queue
    (correctness holds — at-least-once + idempotent guarded updates + single leader for ordering),
    and both KEDA scalers trigger on the same lag → up to ~2× workers (see the prod ResourceQuota
    note). Per-color groups (§5.1's "switch which color's group is scaled") need an app change.
  - *Monitoring ingress:* `allow-monitoring-scrape-apptiers` lets a namespace labelled
    `titan.sh/monitoring: "true"` scrape app-tier metrics (8000/9100) — required for the api
    custom-metric HPA. Label your Prometheus namespace accordingly, or the HPA degrades to CPU-only.
  - *CNPG WAL archiving (§8.2):* added via `Cluster.spec.backup` once the object store exists.
