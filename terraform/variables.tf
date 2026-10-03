variable "aws_region" {
  description = "AWS deployment region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 compute instance size"
  type        = string
  default     = "t3.medium"
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
