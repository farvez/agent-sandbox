# GitHub App for private repository import (optional). Create the app on GitHub (see
# docs/console.md), then set github_app_id, github_app_slug, github_app_client_id,
# github_app_client_secret and github_app_private_key_file in terraform.tfvars.
# The client secret and private key are stored in SSM and fetched when the service starts.

locals {
  github_app_enabled = var.github_app_id != ""
}

resource "aws_ssm_parameter" "github_app_client_secret" {
  count = local.github_app_enabled ? 1 : 0
  name  = "/agent-sandbox/github-app-client-secret"
  type  = "SecureString"
  value = var.github_app_client_secret
}

resource "aws_ssm_parameter" "github_app_private_key" {
  count = local.github_app_enabled ? 1 : 0
  name  = "/agent-sandbox/github-app-private-key"
  type  = "SecureString"
  value = file("${path.module}/${var.github_app_private_key_file}")
}

resource "aws_iam_role_policy" "github_app" {
  count = local.github_app_enabled ? 1 : 0
  name  = "agent-sandbox-github-app"
  role  = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = [aws_ssm_parameter.github_app_client_secret[0].arn, aws_ssm_parameter.github_app_private_key[0].arn]
    }]
  })
}

output "github_app_callback_url" {
  description = "Callback URL to enter in the GitHub App settings"
  value       = "${var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_eip.sandbox.public_ip}"}/console/github/callback"
}
