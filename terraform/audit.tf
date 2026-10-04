# Per-tenant command audit log (GET /v1/audit and the console's Activity page): one item
# per command, partitioned by tenant so each tenant's history is a single Query. DynamoDB
# TTL deletes entries after audit_retention_days.

resource "aws_dynamodb_table" "audit" {
  name                        = "agent-sandbox-audit"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "tenant"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "tenant"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_iam_role_policy" "audit_and_notify" {
  name = "agent-sandbox-audit-notify"
  role = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.audit.arn
      },
      {
        # Console notices (e.g. a new invite request) go to the alert email subscribers.
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
    ]
  })
}
