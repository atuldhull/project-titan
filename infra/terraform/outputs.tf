output "cluster_name" {
  value = module.cluster.name
}

output "cluster_endpoint" {
  value = module.cluster.endpoint
}

output "node_pools" {
  value = module.node_pools.pool_names
}

output "gpu_pools" {
  description = "GPU-tainted pools for the local-inference (Ollama) tier."
  value       = module.node_pools.gpu_pools
}

output "backup_buckets" {
  description = "§8.2 backup target buckets (used by the backup sidecar + DR drill)."
  value       = module.object_storage.bucket_names
}

output "dns_fqdns" {
  value = module.dns.fqdns
}
