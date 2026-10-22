variable "vpc_cidr" {
  description = "Plage CIDR du VPC principal"
  type        = string
  default     = "10.0.0.0/16"
}

variable "environment" {
  description = "Nom de l'environnement (dev, staging, prod...) utilisé pour le tagging"
  type        = string
  default     = "dev"
}
