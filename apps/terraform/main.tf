########################
# Terraform Configuration
########################
terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
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

  az_count = min(2, length(data.aws_availability_zones.available.names))
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

  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node all ports/protocols"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
    
    ingress_cluster_to_node_all_traffic = {
      description                   = "Cluster to node all traffic"
      protocol                      = "-1"
      from_port                     = 0
      to_port                       = 0
      type                          = "ingress"
      source_cluster_security_group = true
    }

    egress_all = {
      description = "Node all egress"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "egress"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

   addons = {
    eks-pod-identity-agent = {
      most_recent = true
      before_compute = true
    }
    aws-ebs-csi-driver = {
      most_recent = true
    }
    coredns = {}
    kube-proxy = {}
    vpc-cni = {
      most_recent = true
      before_compute = true
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
resource "aws_eks_pod_identity_association" "ebs_csi" {
  cluster_name    = module.eks.cluster_name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs_csi_pod_identity_role.arn
}

resource "aws_iam_role" "ebs_csi_pod_identity_role" {
  name = "AmazonEKSTFEBSCSIRole-${local.cluster_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "pods.eks.amazonaws.com"
      }
      Action = [
        "sts:AssumeRole",
        "sts:TagSession"
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi_pod_identity_policy" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi_pod_identity_role.name
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
