# Terraform root for TITAN infrastructure (Vol 9 §2, TITAN-OPS-019). Cloud-AGNOSTIC by design:
# the core here composes four module INTERFACES (cluster, node pools, object storage, DNS) whose
# reference implementations use only Terraform builtins (`terraform_data`) — NO cloud provider is
# baked into core, so "no single vendor is baked in" (ADR-OPS). A production deployment swaps a
# per-cloud implementation (modules/<cloud>/…) behind each identical interface; the same TITAN
# container images + env-based config then run unchanged (ADR-008). `terraform validate` + `fmt`
# run offline (no provider downloads); `terraform plan` needs the per-cloud modules' providers.
terraform {
  required_version = ">= 1.5"
}
