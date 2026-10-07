#!/usr/bin/env bash
# Deploys the current code and configuration to the AWS server.
#
#   scripts/deploy.sh          show the plan, ask, then deploy
#   scripts/deploy.sh --yes    deploy without asking
#
# Code-only changes: terraform uploads the new bundle, then the server updates itself in
# place over SSM. Only the API process restarts (about 30 s); open sessions are kept and
# requests wait at the proxy instead of failing.
#
# Changes to the server's boot script or instance type replace the instance (about 8 min;
# open sessions end). The "API down" alarm is paused meanwhile so it doesn't email you.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: leave /opt/... and /agent-sandbox/... paths alone

cd "$(dirname "$0")/../terraform"
ALARM=agent-sandbox-api-down

terraform plan -out=tfplan -no-color > .plan.txt || { cat .plan.txt; exit 1; }
grep -E "^  # |^Plan:|No changes" .plan.txt || true
REPLACING=0
if grep -q "aws_instance.sandbox_host must be replaced" .plan.txt; then
  REPLACING=1
  echo "The server will be replaced (about 8 minutes; open sessions end)."
fi
rm -f .plan.txt

if [ "${1:-}" != "--yes" ]; then
  read -r -p "Deploy? [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "Cancelled."; exit 1; }
fi

REGION=$(terraform output -raw aws_region 2>/dev/null || echo us-east-1)
ENDPOINT=$(terraform output -raw api_endpoint 2>/dev/null || true)

wait_healthy() {   # seconds
  local deadline=$((SECONDS + $1))
  while [ $SECONDS -lt $deadline ]; do
    if curl -fsS --max-time 10 "$ENDPOINT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 15
  done
  return 1
}

if [ "$REPLACING" = 1 ]; then
  aws cloudwatch disable-alarm-actions --region "$REGION" --alarm-names "$ALARM"
  trap 'aws cloudwatch enable-alarm-actions --region "$REGION" --alarm-names "$ALARM"; echo "Alarm emails re-enabled."' EXIT
  echo "Alarm emails paused during the replacement."
  terraform apply -no-color tfplan | grep -E "Apply complete|Error" || true
  echo "Waiting for the new server (up to 20 minutes)..."
  wait_healthy 1200 && echo "Deployed: $ENDPOINT is healthy." || { echo "The new server isn't healthy yet; check /var/log/user_data.log via SSM." >&2; exit 1; }
  exit 0
fi

terraform apply -no-color tfplan | grep -E "Apply complete|Error" || true
INSTANCE=$(terraform output -raw instance_id)
echo "Updating the code on $INSTANCE in place..."
CMD=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
  --parameters 'commands=["/opt/agent-sandbox/update.sh"]' --query Command.CommandId --output text | tr -d '\r')
aws ssm wait command-executed --region "$REGION" --command-id "$CMD" --instance-id "$INSTANCE" 2>/dev/null || true
STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" --instance-id "$INSTANCE" \
  --query Status --output text | tr -d '\r')
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD" --instance-id "$INSTANCE" \
  --query "[StandardOutputContent,StandardErrorContent]" --output text | sed '/^None$/d'
if [ "$STATUS" != "Success" ]; then
  echo "Update failed ($STATUS); the server rolled back to the previous code." >&2
  exit 1
fi
wait_healthy 120 && echo "Deployed: $ENDPOINT is healthy; open sessions were kept."
