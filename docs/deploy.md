# Deploy to AWS

`terraform/` provisions a VPC, one EC2 instance (Ubuntu 22.04, c7i-flex.large by default —
2 vCPU / 4 GB, eligible on the AWS Free plan) on an Elastic IP, a private S3 bucket holding
the code bundle, generated API keys in SSM, DynamoDB tables for keys and console accounts,
CloudWatch alarms and a cost budget. At boot the host installs Docker, gVisor from its signed
apt repository, AWS CLI and Caddy; builds `sandbox-base:latest` from this repo's Dockerfile;
and runs the API as a non-root `sandbox` user behind Caddy on port 443, with
`SANDBOX_REQUIRE_GVISOR=1`.

## First time only — state bucket

Terraform state contains the API keys, so it lives in an encrypted, versioned S3 bucket with
native locking, not on a laptop:

```bash
cd terraform/bootstrap
terraform init && terraform apply                      # creates agent-sandbox-tfstate-<account-id>
terraform output -raw backend_config > ../backend.hcl  # git-ignored
```

On a new machine, `terraform init -backend-config=backend.hcl` is all it takes to pick up the
existing state. Old state versions are kept for 90 days in the bucket.

## Deploy

Put your settings in `terraform/terraform.tfvars` (git-ignored, so it can hold secrets) so every
`plan` / `apply` / `destroy` uses the same values:

```hcl
domain_name           = "sandbox.example.com"   # DNS A record → the Elastic IP
tenants               = ["default"]
allowed_ingress_cidrs = ["0.0.0.0/0"]           # or ["<your-ip>/32"]
egress_policy         = { "*" = ["pypi"] }      # default internet access for every tenant
alert_email           = "you@example.com"
acme_email            = "you@example.com"       # certificate contact + ZeroSSL fallback
monthly_budget_usd    = 30

# Developer console (optional): a GitHub OAuth app, see console.md
console_admins             = ["your-github-login"]
github_oauth_client_id     = "..."
github_oauth_client_secret = "..."
```

```bash
cd terraform
terraform init -backend-config=backend.hcl
terraform apply
$(terraform output -raw fetch_api_keys_command)   # prints tenant:key pairs
```

Provisioning takes about 8 minutes. Outputs include `api_endpoint`, `console_url` and
`github_oauth_callback_url`.

## Operating it

- **TLS:** with `domain_name` set, Caddy gets a Let's Encrypt certificate; without it Caddy
  serves a self-signed certificate on the IP (`curl -k`). Caddy's certificates and ACME account
  are backed up to the artifacts bucket (`tls/caddy/`, every 10 minutes) and restored on boot,
  so redeploys reuse the certificate instead of requesting a new one (Let's Encrypt allows 5
  per week per domain). `acme_email` adds expiry notices and a ZeroSSL fallback if Let's
  Encrypt refuses.
- **Shell access:** no SSH. Use `aws ssm start-session --target <instance_id>`.
  Provisioning log: `/var/log/user_data.log`; service log: `journalctl -u agent-sandbox`.
- **Stable address:** the API sits on an Elastic IP, so its URL stays the same across
  redeploys; `terraform destroy` releases it.
- **Shipping code changes:** `terraform apply` re-zips `src/` (plus `Dockerfile` and
  `requirements.txt`; never `.env`), so a code change replaces the instance. Sessions are in
  memory and are lost; the URL, keys and console accounts stay.
- **Adding a static tenant:** add its name to `tenants` and `terraform apply`, then restart the
  service (`systemctl restart agent-sandbox` via SSM). Removing a name revokes its key.
  Key rotation: `terraform apply -replace='random_password.tenant_key["acme"]'`, then restart.
  Self-service keys (console or `airlock-sandbox-keys`) need none of this.
- **Self-service keys:** a DynamoDB table (`agent-sandbox-api-keys`, deletion protection and
  point-in-time recovery on) and a generated admin key in SSM. Load it with
  `. .\scripts\demo-env.ps1 -Admin`, or print it with `terraform output -raw fetch_admin_key_command`.
- **Disk quotas:** workspaces live on `/var/lib/agent-sandbox/workspaces`, a loop-mounted
  ext4 filesystem of `workspaces_disk_gb` (default 15 GB) with project quotas, holding
  `workspace_slots` (default 100 = max concurrent sessions) directories of
  `workspace_quota_mb` (default 512 MB) each. Check usage with `sudo repquota -P /var/lib/agent-sandbox/workspaces`.
- **Tenant limits:** `tenant_limits = { "*" = { max_sessions = 10 }, acme = { max_sessions = 50 } }`
  (written to `/etc/agent-sandbox/tenant-limits.json`). Unset = built-in defaults.
- **Egress:** `egress_policy` (default `{}`: no internet for anyone) is written to
  `/etc/agent-sandbox/egress-policy.json`; logs are in `/var/lib/agent-sandbox/egress/`.
  Docker's address pool is widened to `10.210.0.0/16` in /24s (256 concurrent egress sessions).
- **Audit log:** a DynamoDB table (`agent-sandbox-audit`, deletion protection on, TTL after
  `audit_retention_days`, default 90) holds each tenant's command history.
- **Operator emails:** new invite requests are published to the alerts SNS topic, so they reach
  `alert_email` alongside alarms.
- Keys are also stored in Terraform state; keep state private.

## Monitoring

Every minute the host checks `/healthz` through Caddy and TLS and reports `ApiHealthy`,
`ActiveSessions`, `RootDiskUsedPercent` and `WorkspacesDiskUsedPercent` to CloudWatch
(namespace `AgentSandbox`, dimension `Service=agent-sandbox`), and deletes egress logs older
than 30 days. Alarms email `alert_email` (confirm AWS's subscription email first):

| Alarm | Fires when |
|-------|-----------|
| `agent-sandbox-api-down` | health check failing or no data for 3 min (also during the ~8 min of a redeploy) |
| `agent-sandbox-root-disk-80pct` | main disk over 80% |
| `agent-sandbox-workspaces-disk-80pct` | workspace filesystem over 80% |
| `agent-sandbox-status-check-failed` | AWS instance status check failing |
| `agent-sandbox-cpu-high` | CPU over 90% for 15 min |

A monthly cost budget (`monthly_budget_usd`, default $30) emails at 80% of actual spend and
when the month's forecast exceeds 100%.
