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
APP_BUNDLE="${app_bundle_s3}"
API_KEY_PARAM="${api_key_param}"
ADMIN_KEY_PARAM="${admin_key_param}"
KEYS_TABLE="${keys_table}"
DOMAIN="${domain_name}"
SESSION_TTL="${session_ttl_secs}"
EGRESS_LOG_DIR=/var/lib/agent-sandbox/egress
WS_ROOT=/var/lib/agent-sandbox/workspaces
WS_IMAGE=/var/lib/agent-sandbox/workspaces.img
WS_DISK_GB="${workspaces_disk_gb}"
WS_SLOTS="${workspace_slots}"
WS_QUOTA_MB="${workspace_quota_mb}"
APP_DIR=/opt/agent-sandbox
ARCH=$(uname -m)

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

# 5. Application code from the private S3 bundle
mkdir -p "$APP_DIR"
aws s3 cp --region "$AWS_REGION" "$APP_BUNDLE" /tmp/app.zip
unzip -o -q /tmp/app.zip -d "$APP_DIR"

# Sandbox images: build the repo's Dockerfile and pre-pull the other allowlisted
# template (containers.create does not pull missing images).
docker build -t sandbox-base:latest "$APP_DIR"
docker pull python:3.11-slim

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 6. Dedicated service user (in the docker group, not root)
id -u sandbox >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin sandbox
usermod -aG docker sandbox
install -d -o sandbox -g sandbox -m 0700 "$EGRESS_LOG_DIR"

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
exec $APP_DIR/.venv/bin/python3 -m uvicorn src.api.server:app --host 127.0.0.1 --port 8000
EOF
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
Environment=AWS_REGION=$AWS_REGION
Environment=SANDBOX_EGRESS_LOG_DIR=$EGRESS_LOG_DIR
ExecStart=$APP_DIR/start.sh
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now agent-sandbox

# 7. Caddy reverse proxy for TLS
curl -1sLf --retry 5 --retry-all-errors https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf --retry 5 --retry-all-errors https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

if [ -n "$DOMAIN" ]; then
  # Public certificate from Let's Encrypt (needs DNS pointing here and port 80 open)
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8000
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
	reverse_proxy 127.0.0.1:8000
}
EOF
fi

systemctl restart caddy

# 8. Self-monitoring: every minute, check /healthz through Caddy and TLS, report
#    health and disk usage to CloudWatch (alarms in monitoring.tf), and prune
#    egress logs older than 30 days.
cat > /etc/agent-sandbox/monitor.env <<EOF
AWS_REGION=$AWS_REGION
DOMAIN=$DOMAIN
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
