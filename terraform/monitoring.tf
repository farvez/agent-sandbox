# Monitoring: the host reports its own health every minute (monitor.sh in
# user_data) as custom CloudWatch metrics, so the health check works even though
# the firewall only admits allowed_ingress_cidrs. Alarms and the budget notify
# var.alert_email.
#
# Metrics use the dimension Service=agent-sandbox rather than the instance ID, so
# they continue across instance replacements. During a redeploy the API is down
# for ~8 minutes and "api-down" fires, then recovers — expected.

locals {
  metric_namespace = "AgentSandbox"
  metric_dimension = { Service = "agent-sandbox" }
  alarm_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_sns_topic" "alerts" {
  name = "agent-sandbox-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # AWS emails a confirmation link; alerts start after it's clicked
}

# The host may publish metrics, but only into its own namespace.
resource "aws_iam_role_policy" "publish_metrics" {
  name = "agent-sandbox-publish-metrics"
  role = aws_iam_role.sandbox_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["cloudwatch:PutMetricData"]
      Resource  = "*"
      Condition = { StringEquals = { "cloudwatch:namespace" = local.metric_namespace } }
    }]
  })
}

resource "aws_cloudwatch_metric_alarm" "api_down" {
  alarm_name          = "agent-sandbox-api-down"
  alarm_description   = "The API's /healthz (through Caddy and TLS) failed or stopped reporting for 3 minutes."
  namespace           = local.metric_namespace
  metric_name         = "ApiHealthy"
  dimensions          = local.metric_dimension
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching" # a dead host reports nothing — that is the outage
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "root_disk" {
  alarm_name          = "agent-sandbox-root-disk-80pct"
  alarm_description   = "The server's main disk is over 80% full."
  namespace           = local.metric_namespace
  metric_name         = "RootDiskUsedPercent"
  dimensions          = local.metric_dimension
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  treat_missing_data  = "notBreaching" # api_down covers "no data"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "workspaces_disk" {
  alarm_name          = "agent-sandbox-workspaces-disk-80pct"
  alarm_description   = "The workspace filesystem (all sandboxes together) is over 80% full."
  namespace           = local.metric_namespace
  metric_name         = "WorkspacesDiskUsedPercent"
  dimensions          = local.metric_dimension
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "status_check" {
  alarm_name          = "agent-sandbox-status-check-failed"
  alarm_description   = "AWS reports the instance or its host as impaired."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.sandbox_host.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "agent-sandbox-cpu-high"
  alarm_description   = "CPU above 90% for 15 minutes."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.sandbox_host.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 90
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_budgets_budget" "monthly" {
  count        = var.alert_email == "" ? 0 : 1
  name         = "agent-sandbox-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
