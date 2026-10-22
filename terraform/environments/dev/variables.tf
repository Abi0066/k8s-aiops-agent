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
