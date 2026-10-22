provider "aws" {
  region = "us-west-2"
}

module "vpc" {
  source      = "../../modules/vpc"
  vpc_cidr    = var.vpc_cidr
  environment = var.environment
}

output "vpc_id" {
  description = "ID du VPC de l'environnement dev"
  value       = module.vpc.vpc_id
}

output "private_subnets" {
  description = "Sous-réseaux privés du VPC dev"
  value       = module.vpc.private_subnet_ids
}
