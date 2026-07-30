# DNS records → the ingress load balancer — cloud-agnostic INTERFACE. A per-cloud module maps
# each record onto aws_route53_record / google_dns_record_set / azurerm_dns_a_record pointing at
# the ingress LB. Used by the blue/green cutover's final "flip DNS" step (Phase 80 §5.1) and the
# DR drill's step 8.
variable "cloud" { type = string }
variable "zone" { type = string }
variable "target" {
  description = "Ingress load-balancer hostname/IP the records resolve to."
  type        = string
}
variable "records" {
  type = list(object({
    name = string
    type = string
  }))
}

resource "terraform_data" "record" {
  for_each = { for r in var.records : r.name => r }
  input = {
    cloud  = var.cloud
    zone   = var.zone
    name   = each.value.name
    type   = each.value.type
    target = var.target
  }
}

output "fqdns" {
  value = [for r in var.records : "${r.name}.${var.zone}"]
}
