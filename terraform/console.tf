# Developer console at <api_endpoint>/console: GitHub sign-in, API keys, usage.
# Enabled when github_oauth_client_id is set. Accounts, invites and monthly usage
# counters live in one DynamoDB table; the session-signing secret and the GitHub
# OAuth client secret live in SSM and are fetched when the service starts.

locals {
  console_enabled = var.github_oauth_client_id != ""
}

resource "aws_dynamodb_table" "console" {
  name                        = "agent-sandbox-console"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "random_password" "console_secret" {
  length  = 64
  special = false
}

resource "aws_ssm_parameter" "console_secret" {
  name  = "/agent-sandbox/console-secret"
  type  = "SecureString"
  value = random_password.console_secret.result
}

resource "aws_ssm_parameter" "github_client_secret" {
  count = local.console_enabled ? 1 : 0
  name  = "/agent-sandbox/github-client-secret"
  type  = "SecureString"
  value = var.github_oauth_client_secret
}

resource "aws_iam_role_policy" "console" {
  name = "agent-sandbox-console"
  role = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.console.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = concat([aws_ssm_parameter.console_secret.arn], aws_ssm_parameter.github_client_secret[*].arn)
      },
    ]
  })
}

output "console_url" {
  value = local.console_enabled ? "${var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_eip.sandbox.public_ip}"}/console" : "(console disabled: set github_oauth_client_id)"
}

output "github_oauth_callback_url" {
  description = "Authorization callback URL to enter in the GitHub OAuth app"
  value       = "${var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_eip.sandbox.public_ip}"}/console/auth/callback"
}
