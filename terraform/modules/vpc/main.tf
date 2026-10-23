# Module VPC - Multi-AZ résilient avec sous-réseaux publics et privés séparés.
# Une NAT Gateway par AZ pour éliminer le point de défaillance unique.
#
# Le nombre d'AZ vient de var.az_count : il était écrit en dur (`count = 2`)
# dans six ressources, qu'il fallait garder synchronisées à la main.

# 1. VPC Principal
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.environment}-vpc"
  }
}

# 2. Internet Gateway
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.environment}-igw"
  }
}

# Récupération des AZs disponibles dans la région
data "aws_availability_zones" "available" {
  state = "available"
}

# 3. Sous-réseaux Publics (Multi-AZ)
resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.environment}-public-${data.aws_availability_zones.available.names[count.index]}"
  }
}

# 4. Sous-réseaux Privés (Multi-AZ)
resource "aws_subnet" "private" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.environment}-private-${data.aws_availability_zones.available.names[count.index]}"
  }
}

# 5. Elastic IPs et NAT Gateways (Une par AZ pour la Haute Disponibilité)
resource "aws_eip" "nat" {
  count      = var.az_count
  domain     = "vpc"
  depends_on = [aws_internet_gateway.igw]

  tags = {
    Name = "${var.environment}-nat-eip-${count.index + 1}"
  }
}

resource "aws_nat_gateway" "nat" {
  count         = var.az_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${var.environment}-nat-gw-${count.index + 1}"
  }
}

# 6. Tables de routage Publique et Privées
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name = "${var.environment}-rt-public"
  }
}

resource "aws_route_table" "private" {
  count  = var.az_count
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat[count.index].id
  }

  tags = {
    Name = "${var.environment}-rt-private-${count.index + 1}"
  }
}

# 7. Associations des tables de routage
resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}
