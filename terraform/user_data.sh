#!/bin/bash
# Rendered by Terraform templatefile(): template variables use the dollar-brace
# form; every shell variable below deliberately uses plain $VAR (no braces) so
# Terraform leaves it alone.
set -euo pipefail

exec > >(tee /var/log/user_data.log | logger -t user-data -s 2>/dev/console) 2>&1
echo "=== Starting Agent-Sandbox Provisioning ==="

export DEBIAN_FRONTEND=noninteractive
# The Elastic IP is attached a few seconds into boot, briefly swapping the public
# address; retry downloads instead of failing provisioning on that blip.
echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries
AWS_REGION="${aws_region}"
BUNDLE_PARAM="${bundle_param}"
API_KEY_PARAM="${api_key_param}"
ADMIN_KEY_PARAM="${admin_key_param}"
KEYS_TABLE="${keys_table}"
ACCOUNTS_TABLE="${accounts_table}"
CONSOLE_BASE_URL="${console_base_url}"
GITHUB_CLIENT_ID="${github_client_id}"
GITHUB_SECRET_PARAM="${github_secret_param}"
CONSOLE_SECRET_PARAM="${console_secret_param}"
CONSOLE_ADMINS="${console_admins}"
CONSOLE_SIGNUP="${console_signup}"
CONSOLE_CONTACT="${console_contact}"
DOMAIN="${domain_name}"
TLS_STATE_S3="${tls_state_s3}"
ACME_EMAIL="${acme_email}"
AUDIT_TABLE="${audit_table}"
AUDIT_RETENTION_DAYS="${audit_retention_days}"
NOTIFY_TOPIC_ARN="${notify_topic_arn}"
CADDY_DATA=/var/lib/caddy/.local/share/caddy
SESSION_TTL="${session_ttl_secs}"
EGRESS_LOG_DIR=/var/lib/agent-sandbox/egress
WS_ROOT=/var/lib/agent-sandbox/workspaces
WS_IMAGE=/var/lib/agent-sandbox/workspaces.img
WS_DISK_GB="${workspaces_disk_gb}"
WS_SLOTS="${workspace_slots}"
WS_QUOTA_MB="${workspace_quota_mb}"
APP_DIR=/opt/agent-sandbox
ARCH=$(uname -m)

# 0. Keep the system log (which includes the API's request log with client IPs) for 14 days,
#    as the console's privacy policy states.
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]
MaxRetentionSec=14day
' > /etc/systemd/journald.conf.d/retention.conf
systemctl restart systemd-journald

# 1. OS packages
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-release unzip python3-pip python3-venv \
  debian-keyring debian-archive-keyring apt-transport-https

# 2. AWS CLI v2 (to fetch the code bundle and the API key)
curl -fsSL --retry 5 --retry-all-errors "https://awscli.amazonaws.com/awscli-exe-linux-$ARCH.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install

# 3. Docker CE
install -m 0755 -d /etc/apt/keyrings
curl -fsSL --retry 5 --retry-all-errors https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io

# 4. gVisor (runsc) from its official apt repository; apt verifies the package
#    signature. (Standalone runsc binaries are no longer published under latest/.)
echo "=== Installing gVisor (runsc) ==="
curl -fsSL --retry 5 --retry-all-errors https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update -y
apt-get install -y runsc
runsc install   # adds the runsc runtime to /etc/docker/daemon.json
# Each internet-enabled session gets its own Docker network; Docker's default
# pools allow only ~30. 10.210.0.0/16 in /24s allows 256.
python3 - <<'PY'
import json
path = "/etc/docker/daemon.json"
with open(path) as f:
    cfg = json.load(f)
cfg["default-address-pools"] = [{"base": "10.210.0.0/16", "size": 24}]
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
systemctl restart docker
docker info --format '{{json .Runtimes}}' | grep -q runsc   # fail provisioning if not registered

# 5. Application code from the private S3 bundle (its location is an SSM parameter,
#    so later code changes are deployed in place by update.sh, not by a new instance)
mkdir -p "$APP_DIR"
APP_BUNDLE=$(aws ssm get-parameter --region "$AWS_REGION" --name "$BUNDLE_PARAM" --query Parameter.Value --output text)
aws s3 cp --region "$AWS_REGION" "$APP_BUNDLE" /tmp/app.zip
unzip -o -q /tmp/app.zip -d "$APP_DIR"
echo "$APP_BUNDLE" > "$APP_DIR/.bundle"

# The service user exists before the image is built, so the image's "sandbox" user can
# share its uid/gid (workspace files then show as owned by "sandbox", not a number).
id -u sandbox >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin sandbox

# Sandbox images: build the repo's Dockerfile and pre-pull the other allowlisted
# template (containers.create does not pull missing images).
docker build --build-arg SANDBOX_UID="$(id -u sandbox)" --build-arg SANDBOX_GID="$(id -g sandbox)" \
  -t sandbox-base:latest "$APP_DIR"
docker pull python:3.11-slim

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 6. Dedicated service user (created above; in the docker group, not root)
usermod -aG docker sandbox
install -d -o sandbox -g sandbox -m 0700 "$EGRESS_LOG_DIR"
# Open sessions are saved here so they survive API restarts (deploys); holds proxy passes.
install -d -o sandbox -g sandbox -m 0700 /var/lib/agent-sandbox/state

# 6b. Disk-limited workspaces. A dedicated ext4 filesystem (a loop-mounted image,
#     size-capped so workspaces can never fill the main disk) with project quotas,
#     and a fixed set of slot directories, each limited to WS_QUOTA_MB by the
#     kernel. The API claims and wipes slots without needing root.
echo "=== Creating quota-limited workspace slots ==="
apt-get install -y quota "linux-modules-extra-$(uname -r)"
printf 'quota_v2\nquota_tree\n' > /etc/modules-load.d/agent-sandbox-quota.conf   # load at every boot
modprobe quota_v2
modprobe quota_tree
install -d -m 0755 /var/lib/agent-sandbox
fallocate -l "$WS_DISK_GB"G "$WS_IMAGE"
mkfs.ext4 -q -F -m 0 -O quota,project "$WS_IMAGE"
install -d -m 0755 "$WS_ROOT"
echo "$WS_IMAGE $WS_ROOT ext4 loop,prjquota,nodev,nosuid 0 0" >> /etc/fstab
mount "$WS_ROOT"
chmod 0755 "$WS_ROOT"
for i in $(seq 1 "$WS_SLOTS"); do
  slot="$WS_ROOT/slot-$(printf '%03d' "$i")"
  install -d -o sandbox -g sandbox -m 0700 "$slot"
  chattr +P -p "$((10000 + i))" "$slot"
  setquota -P "$((10000 + i))" 0 "$((WS_QUOTA_MB * 1024))" 0 0 "$WS_ROOT"
done
install -d -o sandbox -g sandbox -m 0700 "$WS_ROOT.locks"

# Per-tenant egress policy (Terraform var.egress_policy). Tenants not listed get no internet.
install -d -m 0755 /etc/agent-sandbox
cat > /etc/agent-sandbox/egress-policy.json <<'POLICY'
${egress_policy_json}
POLICY
chmod 0644 /etc/agent-sandbox/egress-policy.json

# Per-tenant limits (Terraform var.tenant_limits): sessions, requests/minute, concurrent commands.
cat > /etc/agent-sandbox/tenant-limits.json <<'LIMITS'
${tenant_limits_json}
LIMITS
chmod 0644 /etc/agent-sandbox/tenant-limits.json

# Launcher fetches the tenant keys from SSM on every start, so they never sit on
# disk; rotating or adding keys only needs a parameter update plus a restart.
cat > "$APP_DIR/start.sh" <<EOF
#!/bin/bash
set -euo pipefail
export SANDBOX_ADMIN_KEY="\$(aws ssm get-parameter --region $AWS_REGION --name $ADMIN_KEY_PARAM \
  --with-decryption --query Parameter.Value --output text)"
export SANDBOX_API_KEYS="\$(aws ssm get-parameter --region $AWS_REGION --name $API_KEY_PARAM \
  --with-decryption --query Parameter.Value --output text)"
EOF
# Developer console (only when a GitHub OAuth app is configured).
if [ -n "$GITHUB_CLIENT_ID" ]; then
  cat >> "$APP_DIR/start.sh" <<EOF
export SANDBOX_CONSOLE_SECRET="\$(aws ssm get-parameter --region $AWS_REGION --name $CONSOLE_SECRET_PARAM \
  --with-decryption --query Parameter.Value --output text)"
export SANDBOX_GITHUB_CLIENT_SECRET="\$(aws ssm get-parameter --region $AWS_REGION --name $GITHUB_SECRET_PARAM \
  --with-decryption --query Parameter.Value --output text)"
export SANDBOX_GITHUB_CLIENT_ID="$GITHUB_CLIENT_ID"
export SANDBOX_CONSOLE_BASE_URL="$CONSOLE_BASE_URL"
export SANDBOX_CONSOLE_ADMINS="$CONSOLE_ADMINS"
export SANDBOX_CONSOLE_SIGNUP="$CONSOLE_SIGNUP"
export SANDBOX_CONSOLE_CONTACT="$CONSOLE_CONTACT"
EOF
fi
echo "exec $APP_DIR/.venv/bin/python3 -m uvicorn src.api.server:app --host 127.0.0.1 --port 8000 --proxy-headers" >> "$APP_DIR/start.sh"
chmod 0755 "$APP_DIR/start.sh"

cat > /etc/systemd/system/agent-sandbox.service <<EOF
[Unit]
Description=Agent Sandbox Execution API
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=sandbox
Group=sandbox
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR
Environment=SANDBOX_REQUIRE_GVISOR=1
Environment=SANDBOX_SESSION_TTL=$SESSION_TTL
Environment=SANDBOX_WORKSPACE_POOL=$WS_ROOT
Environment=SANDBOX_WORKSPACE_QUOTA_MB=$WS_QUOTA_MB
Environment=SANDBOX_EGRESS_POLICY_FILE=/etc/agent-sandbox/egress-policy.json
Environment=SANDBOX_TENANT_LIMITS_FILE=/etc/agent-sandbox/tenant-limits.json
Environment=SANDBOX_KEYSTORE=dynamodb:$KEYS_TABLE
Environment=SANDBOX_ACCOUNTS=dynamodb:$ACCOUNTS_TABLE
Environment=SANDBOX_AUDIT=dynamodb:$AUDIT_TABLE
Environment=SANDBOX_AUDIT_RETENTION_DAYS=$AUDIT_RETENTION_DAYS
Environment=SANDBOX_NOTIFY_TOPIC_ARN=$NOTIFY_TOPIC_ARN
Environment=AWS_REGION=$AWS_REGION
Environment=SANDBOX_EGRESS_LOG_DIR=$EGRESS_LOG_DIR
Environment=SANDBOX_SESSION_STATE=/var/lib/agent-sandbox/state/sessions.json
ExecStart=$APP_DIR/start.sh
Restart=always
RestartSec=5
# A restart (deploy) lets running commands finish (they're capped at 60 s) before stopping.
TimeoutStopSec=90
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now agent-sandbox

# 6c. In-place code updates: scripts/deploy.sh runs this over SSM after terraform has
#     uploaded a new bundle. Only the API process restarts; open sessions are kept.
cat > /etc/agent-sandbox/update.env <<EOF
AWS_REGION=$AWS_REGION
APP_DIR=$APP_DIR
BUNDLE_PARAM=$BUNDLE_PARAM
EOF
cat > "$APP_DIR/update.sh" <<'UPDATE'
#!/bin/bash
set -euo pipefail
. /etc/agent-sandbox/update.env
BUNDLE=$(aws ssm get-parameter --region "$AWS_REGION" --name "$BUNDLE_PARAM" --query Parameter.Value --output text)
if [ "$BUNDLE" = "$(cat "$APP_DIR/.bundle" 2>/dev/null || true)" ]; then
  echo "Already running $BUNDLE"
  exit 0
fi
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
aws s3 cp --region "$AWS_REGION" --only-show-errors "$BUNDLE" "$WORK/app.zip"
unzip -q "$WORK/app.zip" -d "$WORK/new"
if ! cmp -s "$WORK/new/requirements.txt" "$APP_DIR/requirements.txt"; then
  echo "Installing changed requirements"
  "$APP_DIR/.venv/bin/pip" install -q -r "$WORK/new/requirements.txt"
fi
if ! cmp -s "$WORK/new/Dockerfile" "$APP_DIR/Dockerfile"; then
  echo "Rebuilding the sandbox image"
  docker build -q --build-arg SANDBOX_UID="$(id -u sandbox)" --build-arg SANDBOX_GID="$(id -g sandbox)" \
    -t sandbox-base:latest "$WORK/new" >/dev/null
fi
rm -rf "$APP_DIR/src.previous"
mv "$APP_DIR/src" "$APP_DIR/src.previous"
cp -r "$WORK/new/src" "$APP_DIR/src"
cp "$WORK/new/requirements.txt" "$WORK/new/Dockerfile" "$APP_DIR/"
systemctl restart agent-sandbox
for _ in $(seq 1 90); do
  if curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "$BUNDLE" > "$APP_DIR/.bundle"
    echo "Updated to $BUNDLE"
    exit 0
  fi
  sleep 1
done
echo "The new code failed its health check; rolling back" >&2
rm -rf "$APP_DIR/src"
mv "$APP_DIR/src.previous" "$APP_DIR/src"
systemctl restart agent-sandbox
exit 1
UPDATE
chmod 0700 "$APP_DIR/update.sh"

# 7. Caddy reverse proxy for TLS
curl -1sLf --retry 5 --retry-all-errors https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf --retry 5 --retry-all-errors https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

if [ -n "$DOMAIN" ]; then
  # Public certificate from Let's Encrypt (needs DNS pointing here and port 80 open).
  # With a contact email, Caddy falls back to ZeroSSL if Let's Encrypt refuses.
  : > /etc/caddy/Caddyfile
  if [ -n "$ACME_EMAIL" ]; then
    cat > /etc/caddy/Caddyfile <<EOF
{
	email $ACME_EMAIL
}

EOF
  fi
  cat >> /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8000 {
		lb_try_duration 30s
		lb_try_interval 250ms
	}
}
EOF
else
  # No domain: self-signed certificate for the Elastic IP (clients need -k or to trust Caddy's CA)
  PUBLIC_IP="${public_ip}"
  cat > /etc/caddy/Caddyfile <<EOF
{
	default_sni $PUBLIC_IP
}
https://$PUBLIC_IP {
	tls internal
	reverse_proxy 127.0.0.1:8000 {
		lb_try_duration 30s
		lb_try_interval 250ms
	}
}
EOF
fi

# Certificates survive instance replacement: restore Caddy's storage from S3 before it
# starts (otherwise every redeploy requests a new certificate and soon hits Let's
# Encrypt's 5-per-week limit); monitor.sh backs it up again every 10 minutes.
mkdir -p "$CADDY_DATA"
aws s3 sync --region "$AWS_REGION" --only-show-errors "$TLS_STATE_S3/" "$CADDY_DATA/"   || echo "No saved TLS state yet (first deploy)"
chown -R caddy:caddy /var/lib/caddy

systemctl restart caddy

# 8. Self-monitoring: every minute, check /healthz through Caddy and TLS, report
#    health and disk usage to CloudWatch (alarms in monitoring.tf), and prune
#    egress logs older than 30 days.
cat > /etc/agent-sandbox/monitor.env <<EOF
AWS_REGION=$AWS_REGION
DOMAIN=$DOMAIN
TLS_STATE_S3=$TLS_STATE_S3
CADDY_DATA=$CADDY_DATA
EOF

cat > "$APP_DIR/monitor.sh" <<'MON'
#!/bin/bash
set -u
. /etc/agent-sandbox/monitor.env

put() {  # metric value unit
  aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace AgentSandbox \
    --dimensions Service=agent-sandbox --metric-name "$1" --value "$2" --unit "$3"
}

# Through Caddy, so a broken proxy or certificate also counts as down.
if [ -n "$DOMAIN" ]; then
  check=(curl -fsS --max-time 10 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz")
else
  check=(curl -fsSk --max-time 10 "https://127.0.0.1/healthz")
fi

healthy=0
sessions=0
if body=$("$${check[@]}" 2>/dev/null); then
  healthy=1
  sessions=$(printf '%s' "$body" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("active_sessions", 0))' 2>/dev/null || echo 0)
fi

put ApiHealthy "$healthy" Count
put ActiveSessions "$sessions" Count
put RootDiskUsedPercent "$(df --output=pcent / | tail -1 | tr -dc '0-9')" Percent
put WorkspacesDiskUsedPercent "$(df --output=pcent /var/lib/agent-sandbox/workspaces | tail -1 | tr -dc '0-9')" Percent

find /var/lib/agent-sandbox/egress -maxdepth 1 -name '*.jsonl' -mtime +30 -delete

# Back up Caddy's certificates and ACME account every 10 minutes (only changes are uploaded).
if [ $((10#$(date +%M) % 10)) -eq 0 ] && [ -d "$CADDY_DATA/certificates" ]; then
  aws s3 sync --region "$AWS_REGION" --only-show-errors --exclude 'locks/*' "$CADDY_DATA/" "$TLS_STATE_S3/"
fi
MON
chmod 0755 "$APP_DIR/monitor.sh"

cat > /etc/systemd/system/agent-sandbox-monitor.service <<EOF
[Unit]
Description=Agent Sandbox health and disk metrics
After=network-online.target

[Service]
Type=oneshot
ExecStart=$APP_DIR/monitor.sh
EOF

cat > /etc/systemd/system/agent-sandbox-monitor.timer <<'EOF'
[Unit]
Description=Run agent-sandbox-monitor every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now agent-sandbox-monitor.timer

echo "=== Provisioning Complete. Service Ready ==="
