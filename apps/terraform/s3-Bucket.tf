terraform {
  required_version = ">= 1.6.0"
}

provider "aws" {
  region = "us-west-1"
}

resource "aws_s3_bucket" "terraform_state" {
  bucket = "capstone-project-s3"

  force_destroy = false

  tags = {
    Name        = "capstone-project-terraform-state"
    Environment = "capstone"
  }
}

resource "aws_s3_bucket_public_access_block" "block_public" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "encryption" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
