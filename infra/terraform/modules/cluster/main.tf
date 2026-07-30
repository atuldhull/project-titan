# Managed Kubernetes cluster — cloud-agnostic INTERFACE (§4.1). A per-cloud module
# (aws_eks_cluster / google_container_cluster / azurerm_kubernetes_cluster) replaces the
# terraform_data reference below with the real control plane and surfaces the SAME outputs, so
# the root and downstream modules never reference a vendor resource.
variable "cloud" { type = string }
variable "name" { type = string }
variable "region" { type = string }
variable "kubernetes_version" { type = string }

resource "terraform_data" "cluster" {
  input = {
    cloud   = var.cloud
    name    = var.name
    region  = var.region
    version = var.kubernetes_version
  }
}

output "id" {
  description = "Opaque cluster id (a per-cloud module returns the provisioned resource id)."
  value       = terraform_data.cluster.id
}

output "name" {
  value = var.name
}

output "endpoint" {
  description = "Kubernetes API endpoint. Reference value; a per-cloud module returns the real URL."
  value       = "https://${var.name}.${var.region}.${var.cloud}.k8s.internal"
}
