# Composition root — wires the four cloud-agnostic module interfaces (§2/§4). Swapping clouds
# means pointing each module `source` at a per-cloud implementation (modules/<cloud>/…) that
# honours the SAME variables/outputs; nothing here changes.

module "cluster" {
  source             = "./modules/cluster"
  cloud              = var.cloud
  name               = "${var.cluster_name}-${var.environment}"
  region             = var.region
  kubernetes_version = var.kubernetes_version
}

module "node_pools" {
  source     = "./modules/node_pools"
  cloud      = var.cloud
  cluster_id = module.cluster.id
  pools      = var.node_pools
}

module "object_storage" {
  source  = "./modules/object_storage"
  cloud   = var.cloud
  buckets = var.backup_buckets
}

module "dns" {
  source  = "./modules/dns"
  cloud   = var.cloud
  zone    = var.dns_zone
  records = var.dns_records
  # Reference target; production wires the ingress LB hostname a per-cloud cluster module outputs.
  target = module.cluster.endpoint
}
