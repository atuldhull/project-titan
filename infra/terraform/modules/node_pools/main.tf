# Worker node pools (§4.4). Cloud-agnostic INTERFACE — a per-cloud module maps each pool onto
# its managed node group / VMSS / node pool with the cluster autoscaler wired to [min,max]. The
# GPU pool (gpu = true) is TAINTED so ONLY the local-inference (Ollama) pods, which tolerate it,
# schedule there (§4.2) — no general workload lands on expensive GPU nodes.
variable "cloud" { type = string }
variable "cluster_id" { type = string }
variable "pools" {
  type = list(object({
    name          = string
    instance_type = string
    min_size      = number
    max_size      = number
    gpu           = optional(bool, false)
    labels        = optional(map(string), {})
    taints        = optional(list(object({ key = string, value = string, effect = string })), [])
  }))
}

# A GPU pool must carry a NoSchedule taint, or general pods flood the GPU nodes.
resource "terraform_data" "pool_guard" {
  for_each = { for p in var.pools : p.name => p if p.gpu }
  input    = each.value
  lifecycle {
    precondition {
      condition     = anytrue([for t in each.value.taints : t.effect == "NoSchedule" || t.effect == "NO_SCHEDULE"])
      error_message = "GPU pool '${each.value.name}' must carry a NoSchedule taint (else non-GPU pods schedule onto GPU nodes)."
    }
  }
}

resource "terraform_data" "pool" {
  for_each = { for p in var.pools : p.name => p }
  input = {
    cluster = var.cluster_id
    pool    = each.value
  }
}

output "pool_names" {
  value = [for p in var.pools : p.name]
}

output "gpu_pools" {
  description = "Names of the GPU-tainted pools (the Ollama tier)."
  value       = [for p in var.pools : p.name if p.gpu]
}
