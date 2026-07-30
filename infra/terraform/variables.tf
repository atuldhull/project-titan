# Root inputs — cloud-agnostic (a per-cloud module maps these onto its own resources).

variable "cloud" {
  description = "Target substrate the per-cloud modules implement (aws | gcp | azure | kind). Core stays vendor-neutral; this only selects which implementation an operator wires in."
  type        = string
  default     = "kind"
  validation {
    condition     = contains(["aws", "gcp", "azure", "kind"], var.cloud)
    error_message = "cloud must be one of: aws, gcp, azure, kind."
  }
}

variable "environment" {
  description = "Deployment environment (drives naming + the namespace TITAN runs in)."
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be 'staging' or 'production'."
  }
}

variable "region" {
  description = "Cloud region / location for the cluster and buckets."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "Managed Kubernetes cluster name."
  type        = string
  default     = "titan"
}

variable "kubernetes_version" {
  description = "Kubernetes minor version for the managed control plane."
  type        = string
  default     = "1.30"
}

variable "node_pools" {
  description = <<-EOT
    Worker node pools (§4.4 capacity). Each: name, instance_type, min/max size (the cluster
    autoscaler range), optional labels, and taints. The GPU pool for the local-inference (Ollama)
    tier is a SEPARATE pool — set gpu = true and it is tainted so only GPU-tolerant pods (the
    Ollama pool) land on it (§4.2 note).
  EOT
  type = list(object({
    name          = string
    instance_type = string
    min_size      = number
    max_size      = number
    gpu           = optional(bool, false)
    labels        = optional(map(string), {})
    taints        = optional(list(object({ key = string, value = string, effect = string })), [])
  }))
  default = [
    { name = "system", instance_type = "m5.large", min_size = 2, max_size = 4 },
    { name = "workers", instance_type = "m5.xlarge", min_size = 2, max_size = 10 },
    {
      name          = "gpu-ollama"
      instance_type = "g5.xlarge"
      min_size      = 0
      max_size      = 3
      gpu           = true
      labels        = { "titan.sh/pool" = "ollama" }
      taints        = [{ key = "nvidia.com/gpu", value = "present", effect = "NO_SCHEDULE" }]
    },
  ]
}

variable "backup_buckets" {
  description = <<-EOT
    S3-compatible object stores for the §8.2 backup targets (Postgres base backups + WAL archive,
    Qdrant snapshots, age-sealed audit archive). Versioning + a retention floor are ON so a
    ransomware/operator delete cannot destroy the last good backup within the retention window
    (RPO ≤ 5 min needs the WAL archive intact).
  EOT
  type = list(object({
    name           = string
    retention_days = number
    versioning     = optional(bool, true)
    object_lock    = optional(bool, true)
  }))
  default = [
    { name = "titan-pg-backups", retention_days = 30 },
    { name = "titan-qdrant-snapshots", retention_days = 14 },
    { name = "titan-audit-archive", retention_days = 400 }, # §Vol8 audit retention
  ]
}

variable "dns_zone" {
  description = "DNS zone TITAN's ingress hostnames live under (e.g. titan.example.com)."
  type        = string
  default     = "titan.example.com"
}

variable "dns_records" {
  description = "A/CNAME records to point at the ingress load balancer (name → the ingress hostname/IP output)."
  type = list(object({
    name = string
    type = string
  }))
  default = [
    { name = "app", type = "A" },
    { name = "api", type = "A" },
  ]
}
