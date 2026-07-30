# TITAN infrastructure (Terraform) — Vol 9 §2, TITAN-OPS-019

Cloud-**agnostic** IaC for the substrate TITAN runs on: the managed Kubernetes cluster, its node
pools (including the GPU pool for local inference), the S3-compatible backup buckets (§8.2), and
DNS. Once the substrate exists, the app itself is deployed by the Phase-79 kustomize base +
Phase-80 blue/green release scripts — the same container images, env-configured (ADR-008).

## Design: interfaces in core, vendors at the edge

The root composes **four module interfaces** — `cluster`, `node_pools`, `object_storage`, `dns` —
whose reference implementations use only Terraform builtins (`terraform_data`). **No cloud
provider is baked into core**, so no single vendor is either. A production deployment points each
module `source` at a per-cloud implementation under `modules/<cloud>/…` (e.g. `aws_eks_cluster`,
`aws_s3_bucket` + object-lock, `aws_route53_record`) that honours the **same variables and
outputs** — the root, and everything downstream, is unchanged. `cloud` (`aws|gcp|azure|kind`) is
a label the operator uses to select that implementation.

```
infra/terraform/
  versions.tf variables.tf main.tf outputs.tf     # cloud-agnostic root
  modules/
    cluster/         managed k8s control plane      → endpoint, id, name
    node_pools/      worker pools + tainted GPU pool → pool_names, gpu_pools
    object_storage/  §8.2 backup buckets (WORM)      → bucket_names, endpoints
    dns/             records → ingress LB            → fqdns
  terraform.tfvars.example
```

Guardrails encoded as `precondition`s (fail at plan, not in prod): a GPU pool MUST carry a
`NoSchedule` taint (else general pods flood GPU nodes); a backup bucket's `retention_days` MUST be
≥ 7 (a backup you can't restore from is no backup). Backup buckets default to versioning +
object-lock ON so a delete/ransomware can't destroy the last good backup within the retention
window — the RPO ≤ 5 min guarantee depends on the WAL archive surviving.

## Validate & format

```bash
terraform -chdir=infra/terraform fmt -check -recursive     # style (offline)
terraform -chdir=infra/terraform init -backend=false       # no providers for the reference impl
terraform -chdir=infra/terraform validate                  # config + type + guardrail checks
```

## Verified vs. cloud-only

- **Verified locally + in CI:** `fmt -check`, `init -backend=false`, `validate` on the
  cloud-agnostic core (offline — the reference impl needs no provider). `terraform validate and
  plan run clean` (OPS-019 DoD) at the interface level.
- **Cloud-only (needs a per-cloud module + credentials):** a real `plan`/`apply` that provisions
  a cluster/buckets/DNS. The per-cloud modules are the vendor edge; they are added per deployment
  target and validated in that environment (no cloud credentials or provider are committed here).
