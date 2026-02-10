
# Create an SSH keypair locally and register the public key with AWS
variable "platform_instance_count" {
	description = "Number of EC2 instances to create"
	type        = number
	default     = 2
}

variable "private_key_output_path" {
	description = "Where to write the generated private key on the machine running Terraform (DO NOT commit this file)."
	type        = string
	default     = "./platformapi_id_rsa"
}

resource "tls_private_key" "platformapi" {
	algorithm = "RSA"
	rsa_bits  = 4096
}

resource "aws_key_pair" "platformapi" {
	key_name   = "platformapi-key"
	public_key = tls_private_key.platformapi.public_key_openssh
}

resource "local_file" "private_key" {
	content              = tls_private_key.platformapi.private_key_pem
	filename             = var.private_key_output_path
	file_permission      = "0600"
	directory_permission = "0700"
}

data "aws_ami" "amazon_linux2" {
	most_recent = true
	owners      = ["amazon"]

	filter {
		name   = "name"
		values = ["al2023-ami-*-x86_64*"]
	}
}

resource "aws_instance" "platformapi" {
	count                       = var.platform_instance_count
	ami                         = data.aws_ami.amazon_linux_2.id
	instance_type               = "t3.micro"
	subnet_id                   = element(module.vpc.public_subnets, count.index % length(module.vpc.public_subnets))
	vpc_security_group_ids      = [aws_security_group.allow_all.id]
	key_name                    = aws_key_pair.platformapi.key_name
	associate_public_ip_address = true

	tags = {
		Name = "${local.cluster_name}-platformapi-${count.index}"
	}
}

output "platformapi_public_ips" {
	value       = aws_instance.platformapi[*].public_ip
	description = "Public IP addresses of the platformapi EC2 instances"
}

output "platformapi_key_name" {
	value       = aws_key_pair.platformapi.key_name
	description = "Name of the EC2 Key Pair created for platformapi instances"
}

output "private_key_path" {
	value       = local_file.private_key.filename
	description = "Path to the private key file created on the machine running terraform (sensitive)"
}

