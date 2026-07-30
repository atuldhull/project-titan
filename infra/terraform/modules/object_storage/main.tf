# S3-compatible object stores for the §8.2 backup targets — cloud-agnostic INTERFACE. A
# per-cloud module maps each bucket onto aws_s3_bucket / google_storage_bucket / azurerm_storage
# _container with versioning + object-lock (WORM) retention. Versioning + object_lock default ON
# so a delete/ransomware cannot destroy the last good backup inside the retention window — the
# RPO ≤ 5 min guarantee depends on the WAL archive staying intact (OPS-019).
variable "cloud" { type = string }
variable "buckets" {
  type = list(object({
    name           = string
    retention_days = number
    versioning     = optional(bool, true)
    object_lock    = optional(bool, true)
  }))
}

resource "terraform_data" "bucket" {
  for_each = { for b in var.buckets : b.name => b }
  input = {
    cloud          = var.cloud
    name           = each.value.name
    retention_days = each.value.retention_days
    versioning     = each.value.versioning
    object_lock    = each.value.object_lock
  }
  lifecycle {
    precondition {
      condition     = each.value.retention_days >= 7
      error_message = "backup bucket '${each.value.name}' retention_days must be >= 7 (a backup you can't restore from is no backup)."
    }
  }
}

output "bucket_names" {
  value = [for b in var.buckets : b.name]
}

output "endpoints" {
  description = "s3://-style URIs the backup sidecar targets (a per-cloud module returns real endpoints)."
  value       = { for b in var.buckets : b.name => "s3://${b.name}" }
}
