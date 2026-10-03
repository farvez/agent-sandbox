# One-time setup: the S3 bucket that holds the main configuration's Terraform state.
#
#   cd terraform/bootstrap && terraform init && terraform apply
#
# This configuration keeps its own (tiny, secret-free) state locally; it only
# describes the bucket. The main configuration's state — which contains the API
# keys — lives in the bucket, encrypted and versioned.

terraform {
  required_version = ">= 1.10.0" # S3-native state locking (use_lockfile)
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "tfstate" {
  bucket = "agent-sandbox-tfstate-${data.aws_caller_identity.current.account_id}"

  # Losing this bucket means losing track of the deployed infrastructure.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled" # every state change can be rolled back
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# Refuse any access that isn't over HTTPS.
resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.tfstate.arn, "${aws_s3_bucket.tfstate.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "backend_config" {
  description = "Contents for terraform/backend.hcl"
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.bucket}"
    key          = "agent-sandbox/terraform.tfstate"
    region       = "${var.aws_region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
