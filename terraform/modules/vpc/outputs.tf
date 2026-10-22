output "vpc_id" {
  description = "ID du VPC créé par le module"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "Liste des IDs des sous-réseaux privés (un par AZ)"
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "Liste des IDs des sous-réseaux publics (un par AZ)"
  value       = aws_subnet.public[*].id
}
