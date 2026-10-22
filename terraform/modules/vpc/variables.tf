variable "vpc_cidr" {
  description = "Plage CIDR du VPC principal"
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr doit être un bloc CIDR IPv4 valide (ex. 10.0.0.0/16)."
  }
}

variable "environment" {
  description = "Nom de l'environnement (dev, staging, prod...) utilisé pour le tagging"
  type        = string
  default     = "dev"
}

variable "az_count" {
  description = "Nombre de zones de disponibilité couvertes (un sous-réseau public, un privé et une NAT Gateway par AZ)"
  type        = number
  default     = 2

  # Les sous-réseaux privés commencent à l'index 10 (voir main.tf) : au-delà
  # de 10 AZ, publics et privés se chevaucheraient.
  validation {
    condition     = var.az_count >= 1 && var.az_count <= 10
    error_message = "az_count doit être compris entre 1 et 10."
  }
}
