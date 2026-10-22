output "vpc_id" {
  description = "ID du VPC de l'environnement dev"
  value       = module.vpc.vpc_id
}

output "private_subnets" {
  description = "Sous-réseaux privés du VPC dev"
  value       = module.vpc.private_subnet_ids
}

output "public_subnets" {
  description = "Sous-réseaux publics du VPC dev"
  value       = module.vpc.public_subnet_ids
}
