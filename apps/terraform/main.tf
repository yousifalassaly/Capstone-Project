########################
# Terraform Configuration
########################
terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
    bucket  = "s3-project-for-capstone"
    key     = "state/capstone-project/terraform.tfstate"
    region  = "us-west-1"
    encrypt = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

########################
# Variables
########################
variable "region" {
  default = "us-west-1"
}

variable "cluster_name" {
  default = "capstone-project"
}

########################
# Provider
########################
provider "aws" {
  region = var.region
}

########################
# Availability Zones
########################
data "aws_availability_zones" "available" {
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

########################
# Locals (AZ-safe)
########################
locals {
  cluster_name = var.cluster_name
  vpc_name     = "${var.cluster_name}-vpc"

  az_count = min(3, length(data.aws_availability_zones.available.names))
  azs      = slice(data.aws_availability_zones.available.names, 0, local.az_count)

  private_subnets = slice(
    ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"],
    0,
    local.az_count
  )

  public_subnets = slice(
    ["10.0.4.0/24", "10.0.5.0/24", "10.0.6.0/24"],
    0,
    local.az_count
  )
}

########################
# VPC
########################
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.8.1"

  name = local.vpc_name
  cidr = "10.0.0.0/16"

  azs             = local.azs
  private_subnets = local.private_subnets
  public_subnets  = local.public_subnets

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

########################
# Security Group (LAB)
########################
resource "aws_security_group" "allow_all" {
  name        = "${local.cluster_name}-allow-all"
  description = "Allow all inbound traffic - LAB ONLY"
  vpc_id      = module.vpc.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

########################
# EKS
########################
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = local.cluster_name
  kubernetes_version = "1.33"

  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  addons = {
    eks-pod-identity-agent = {
      most_recent = true
    }
    aws-ebs-csi-driver = {
      service_account_role_arn = module.irsa-ebs-csi.iam_role_arn
    }
  }

  eks_managed_node_groups = {
    main = {
      name           = "capstone-node-group"
      instance_types = ["t3.large"]
      ami_type       = "AL2023_x86_64_STANDARD"

      min_size     = local.az_count
      max_size     = local.az_count
      desired_size = local.az_count
    }
  }
}

########################
# IRSA (EBS CSI)
########################
data "aws_iam_policy" "ebs_csi_policy" {
  arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

module "irsa-ebs-csi" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-assumable-role-with-oidc"
  version = "5.39.0"

  create_role                   = true
  role_name                     = "AmazonEKSTFEBSCSIRole-${module.eks.cluster_name}"
  provider_url                  = module.eks.oidc_provider
  role_policy_arns              = [data.aws_iam_policy.ebs_csi_policy.arn]
  oidc_fully_qualified_subjects = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
}

########################
# Outputs
########################
output "cluster_name" {
  value = module.eks.cluster_name
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
