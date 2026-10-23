# Environnement dev : un VPC multi-AZ, socle réseau d'un futur cluster EKS.
#
# Les outputs sont déclarés dans outputs.tf (et seulement là : Terraform
# refuse deux blocs `output` du même nom dans un module).

terraform {
  # `domain = "vpc"` sur aws_eip n'existe qu'à partir du provider AWS 5.0.
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Tags appliqués à TOUTES les ressources : on retrouve d'un coup d'œil ce
  # qui appartient au projet dans la console AWS (et dans la facture).
  default_tags {
    tags = {
      Project     = "k8s-aiops-agent"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

module "vpc" {
  source      = "../../modules/vpc"
  vpc_cidr    = var.vpc_cidr
  environment = var.environment
  az_count    = var.az_count
}
