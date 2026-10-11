# Workspace snapshots (POST /v1/sessions/{id}/snapshots): compressed archives of a session's
# /workspace, one folder per tenant, deleted by a lifecycle rule after snapshot_retention_days.

resource "aws_s3_bucket" "snapshots" {
  bucket_prefix = "agent-sandbox-snapshots-"
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id

  rule {
    id     = "expire-snapshots"
    status = "Enabled"
    filter {
      prefix = "snapshots/"
    }
    expiration {
      days = var.snapshot_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_policy" "snapshots_tls_only" {
  bucket = aws_s3_bucket.snapshots.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.snapshots.arn, "${aws_s3_bucket.snapshots.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.snapshots]
}

# Settings the API reads at startup (src/api/runtime_config.py). New settings go here rather
# than into the boot script, which would replace the host when it changes.
resource "aws_ssm_parameter" "runtime_config" {
  name = "/agent-sandbox/runtime-config"
  type = "String"
  value = jsonencode({
    SANDBOX_SNAPSHOTS               = "s3://${aws_s3_bucket.snapshots.id}/snapshots"
    SANDBOX_SNAPSHOT_RETENTION_DAYS = tostring(var.snapshot_retention_days)
    SANDBOX_SNAPSHOT_LIMIT          = tostring(var.snapshot_limit)
    SANDBOX_SNAPSHOT_MAX_MB         = tostring(var.snapshot_max_mb)
    SANDBOX_PREVIEW_DOMAIN          = var.preview_domain
    SANDBOX_MAX_PERSISTENT_SESSIONS = tostring(var.max_persistent_sessions)
    # Who may reach which hosts, and per-tenant limits: changing these restarts the API only.
    SANDBOX_EGRESS_POLICY = jsonencode(var.egress_policy)
    SANDBOX_TENANT_LIMITS = jsonencode(var.tenant_limits)
  })
}

resource "aws_iam_role_policy" "snapshots" {
  name = "agent-sandbox-snapshots"
  role = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.snapshots.arn}/snapshots/*"
      },
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.snapshots.arn
        Condition = { StringLike = { "s3:prefix" = ["snapshots/*"] } }
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.runtime_config.arn
      },
    ]
  })
}
