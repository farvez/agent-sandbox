terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# 1. Dedicated VPC & Networking
resource "aws_vpc" "sandbox_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "agent-sandbox-vpc"
  }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.sandbox_vpc.id

  tags = {
    Name = "agent-sandbox-igw"
  }
}

resource "aws_subnet" "public_subnet" {
  vpc_id                  = aws_vpc.sandbox_vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.aws_region}a"

  tags = {
    Name = "agent-sandbox-public-subnet"
  }
}

resource "aws_route_table" "public_rt" {
  vpc_id = aws_vpc.sandbox_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name = "agent-sandbox-public-rt"
  }
}

resource "aws_route_table_association" "public_rta" {
  subnet_id      = aws_subnet.public_subnet.id
  route_table_id = aws_route_table.public_rt.id
}

# 2. Security Group: HTTPS only. No SSH; use SSM Session Manager for shell access.
resource "aws_security_group" "sandbox_sg" {
  name        = "agent-sandbox-sg"
  description = "HTTPS ingress for the Sandbox REST API"
  vpc_id      = aws_vpc.sandbox_vpc.id

  ingress {
    description = "Sandbox API over HTTPS (Caddy)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidrs
  }

  # Let's Encrypt HTTP-01 challenges come from anywhere, so port 80 is opened
  # only when a domain is configured. Caddy just redirects it to HTTPS.
  dynamic "ingress" {
    for_each = var.domain_name == "" ? [] : [1]
    content {
      description = "ACME HTTP-01 challenge and HTTPS redirect"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # Host egress for apt, Docker images, S3 and SSM. Sandboxed containers
  # themselves run with network_mode=none.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "agent-sandbox-sg"
  }
}

# 3. Application bundle: an explicit allowlist so .env, .venv and .git never ship.
data "archive_file" "app" {
  type        = "zip"
  output_path = "${path.module}/.build/app.zip"

  dynamic "source" {
    for_each = setunion(
      fileset("${path.module}/..", "src/**/*.py"),
      ["Dockerfile", "requirements.txt"],
    )
    content {
      content  = file("${path.module}/../${source.value}")
      filename = source.value
    }
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "agent-sandbox-artifacts-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "app" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "app-${data.archive_file.app.output_md5}.zip"
  source = data.archive_file.app.output_path
  etag   = data.archive_file.app.output_md5
}

# 4. One generated API key per tenant, stored together as an SSM SecureString in the
#    SANDBOX_API_KEYS format ("tenant:key,tenant:key") and fetched by the host each
#    time the service starts. Keys are also in Terraform state; keep state private.
resource "random_password" "tenant_key" {
  for_each = toset(var.tenants)
  length   = 48
  special  = false
}

resource "aws_ssm_parameter" "api_keys" {
  name  = "/agent-sandbox/api-keys"
  type  = "SecureString"
  value = join(",", [for t in var.tenants : "${t}:${random_password.tenant_key[t].result}"])
}

# 5. Instance role: read the bundle and the key, and allow SSM Session Manager.
resource "aws_iam_role" "sandbox_host" {
  name_prefix = "agent-sandbox-host-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.sandbox_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "sandbox_host" {
  name = "agent-sandbox-host"
  role = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.api_keys.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "sandbox_host" {
  name_prefix = "agent-sandbox-host-"
  role        = aws_iam_role.sandbox_host.name
}

# 6. Latest official Ubuntu 22.04 LTS AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# 7. EC2 host
resource "aws_instance" "sandbox_host" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public_subnet.id
  vpc_security_group_ids      = [aws_security_group.sandbox_sg.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.sandbox_host.name

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    aws_region         = var.aws_region
    app_bundle_s3      = "s3://${aws_s3_bucket.artifacts.id}/${aws_s3_object.app.key}"
    api_key_param      = aws_ssm_parameter.api_keys.name
    domain_name        = var.domain_name
    session_ttl_secs   = var.session_ttl_seconds
    egress_policy_json = jsonencode(var.egress_policy)
  })

  # A new code bundle produces new user_data, which replaces the host.
  user_data_replace_on_change = true

  tags = {
    Name = "agent-sandbox-runtime-host"
  }
}
