#!/bin/bash
# Rendered by Terraform templatefile(): template variables use the dollar-brace
# form; every shell variable below deliberately uses plain $VAR (no braces) so
# Terraform leaves it alone.
set -euo pipefail

exec > >(tee /var/log/user_data.log | logger -t user-data -s 2>/dev/console) 2>&1
echo "=== Starting Agent-Sandbox Provisioning ==="

export DEBIAN_FRONTEND=noninteractive
AWS_REGION="${aws_region}"
APP_BUNDLE="${app_bundle_s3}"
API_KEY_PARAM="${api_key_param}"
DOMAIN="${domain_name}"
SESSION_TTL="${session_ttl_secs}"
EGRESS_LOG_DIR=/var/lib/agent-sandbox/egress
APP_DIR=/opt/agent-sandbox
ARCH=$(uname -m)

# 1. OS packages
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-release unzip python3-pip python3-venv \
  debian-keyring debian-archive-keyring apt-transport-https

# 2. AWS CLI v2 (to fetch the code bundle and the API key)
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$ARCH.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install

# 3. Docker CE
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io

# 4. gVisor (runsc) from its official apt repository; apt verifies the package
#    signature. (Standalone runsc binaries are no longer published under latest/.)
echo "=== Installing gVisor (runsc) ==="
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
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
install -d -o sandbox -g sandbox -m 0700 /var/lib/agent-sandbox/workspaces
install -d -o sandbox -g sandbox -m 0700 "$EGRESS_LOG_DIR"

# Per-tenant egress policy (Terraform var.egress_policy). Tenants not listed get no internet.
install -d -m 0755 /etc/agent-sandbox
cat > /etc/agent-sandbox/egress-policy.json <<'POLICY'
${egress_policy_json}
POLICY
chmod 0644 /etc/agent-sandbox/egress-policy.json

# Launcher fetches the tenant keys from SSM on every start, so they never sit on
# disk; rotating or adding keys only needs a parameter update plus a restart.
cat > "$APP_DIR/start.sh" <<EOF
#!/bin/bash
set -euo pipefail
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
Environment=TMPDIR=/var/lib/agent-sandbox/workspaces
Environment=SANDBOX_EGRESS_POLICY_FILE=/etc/agent-sandbox/egress-policy.json
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
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
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
  # No domain: self-signed certificate for the public IP (clients need -k or to trust Caddy's CA)
  IMDS_TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
  PUBLIC_IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
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

echo "=== Provisioning Complete. Service Ready ==="
