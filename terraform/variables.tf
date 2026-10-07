variable "aws_region" {
  description = "AWS deployment region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance size (x86_64). c7i-flex.large = 2 vCPU / 4 GB and is eligible on the AWS Free plan."
  type        = string
  default     = "c7i-flex.large"
}

variable "tenants" {
  description = "Tenant names; each gets its own generated API key. Adding a name issues a new key."
  type        = list(string)
  default     = ["default"]

  validation {
    condition     = length(var.tenants) > 0 && alltrue([for t in var.tenants : can(regex("^[a-z0-9][a-z0-9_-]{0,62}$", t))])
    error_message = "Tenant names must be 1-63 chars of lowercase letters, digits, '-' or '_'."
  }
}

variable "allowed_ingress_cidrs" {
  description = "CIDR blocks permitted to call the Sandbox API on 443 (limit to your IP or VPC)"
  type        = list(string)
  default     = ["127.0.0.1/32"] # Default to restricted, override explicitly
}

variable "domain_name" {
  description = "DNS name pointing at the instance, for a Let's Encrypt certificate. Empty = self-signed cert on the IP."
  type        = string
  default     = ""
}

variable "session_ttl_seconds" {
  description = "Idle seconds before a sandbox session is reaped"
  type        = number
  default     = 1800
}

variable "egress_policy" {
  description = "Hosts each tenant's sandboxes may reach over HTTPS via the egress gateway, e.g. { default = [\"pypi\"] }. Presets: pypi, npm, github, huggingface. Tenants not listed get no internet."
  type        = map(list(string))
  default     = {}
}

variable "workspace_quota_mb" {
  description = "Disk quota per sandbox session workspace, enforced by the kernel (ext4 project quota)."
  type        = number
  default     = 512
}

variable "workspace_slots" {
  description = "Maximum concurrent sandbox sessions (one quota-limited workspace slot each)."
  type        = number
  default     = 100

  validation {
    condition     = var.workspace_slots >= 1 && var.workspace_slots <= 999
    error_message = "workspace_slots must be between 1 and 999."
  }
}

variable "workspaces_disk_gb" {
  description = "Total size of the workspace filesystem; caps all workspaces together (taken from the 30 GB root volume)."
  type        = number
  default     = 15

  validation {
    condition     = var.workspaces_disk_gb >= 1 && var.workspaces_disk_gb <= 20
    error_message = "workspaces_disk_gb must be 1-20 (the root volume is 30 GB and also holds the OS and Docker images)."
  }
}

variable "tenant_limits" {
  description = "Per-tenant limits; \"*\" sets defaults for all tenants. Keys: max_sessions, requests_per_minute, max_concurrent_exec. Unset = built-in defaults (10, 120, 4)."
  type        = map(map(number))
  default     = {}
  # Example: { "*" = { max_sessions = 10 }, acme = { max_sessions = 50, requests_per_minute = 600 } }
}

variable "alert_email" {
  description = "Email for alarms (API down, disk filling, unhealthy server, high CPU) and budget warnings. Empty = alarms still exist but notify no one. AWS sends a confirmation link first."
  type        = string
  default     = ""
}

variable "console_contact" {
  description = "Contact shown on the console's privacy and terms pages (an email address). Empty = point people to GitHub issues."
  type        = string
  default     = ""
}

variable "audit_retention_days" {
  description = "Days the per-tenant command audit log keeps entries (DynamoDB TTL deletes older ones)."
  type        = number
  default     = 90

  validation {
    condition     = var.audit_retention_days >= 1 && var.audit_retention_days <= 3650
    error_message = "audit_retention_days must be between 1 and 3650."
  }
}

variable "acme_email" {
  description = "Contact email for TLS certificates (Let's Encrypt expiry notices). Setting it also lets Caddy fall back to ZeroSSL when Let's Encrypt refuses, e.g. its 5-certificates-per-week limit."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget; an email goes out at 80% of actual spend and when the forecast exceeds 100%."
  type        = number
  default     = 30
}

variable "github_oauth_client_id" {
  description = "Client ID of the GitHub OAuth app used for console sign-in. Empty = console disabled."
  type        = string
  default     = ""
}

variable "github_oauth_client_secret" {
  description = "Client secret of that GitHub OAuth app (stored in SSM). Keep it in terraform.tfvars, which is git-ignored."
  type        = string
  default     = ""
  sensitive   = true
}

variable "console_admins" {
  description = "GitHub usernames with console admin rights (invite people, see users). Admins can always sign in."
  type        = list(string)
  default     = []
}

variable "console_signup" {
  description = "\"invite\": only admins and invited GitHub users can sign in. \"open\": anyone with a GitHub account."
  type        = string
  default     = "invite"

  validation {
    condition     = contains(["invite", "open"], var.console_signup)
    error_message = "console_signup must be \"invite\" or \"open\"."
  }
}
