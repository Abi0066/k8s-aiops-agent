variable "aws_region" {
  description = "Région AWS de l'environnement dev"
  type        = string
  default     = "us-west-2"
}

variable "vpc_cidr" {
  description = "Plage CIDR du VPC pour l'environnement dev"
  type        = string
  default     = "10.0.0.0/16"
}

variable "environment" {
  description = "Nom de l'environnement"
  type        = string
  default     = "dev"
}

variable "az_count" {
  description = "Nombre de zones de disponibilité (une NAT Gateway par AZ, ~32 $/mois chacune)"
  type        = number
  default     = 2
}
