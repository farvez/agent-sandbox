output "fetch_api_keys_command" {
  description = "Prints the tenant:key pairs (needs ssm:GetParameter on the parameter)"
  value       = "aws ssm get-parameter --region ${var.aws_region} --name ${aws_ssm_parameter.api_keys.name} --with-decryption --query Parameter.Value --output text"
}

output "instance_id" {
  description = "EC2 instance ID (shell access: aws ssm start-session --target <id>)"
  value       = aws_instance.sandbox_host.id
}

output "instance_public_ip" {
  description = "Public IPv4 address of the sandbox EC2 host"
  value       = aws_eip.sandbox.public_ip
}

output "api_endpoint" {
  description = "HTTPS API base URL"
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "https://${aws_eip.sandbox.public_ip}"
}

output "test_curl_command" {
  description = "Health check (-k only needed for the self-signed certificate when no domain is set)"
  value = (var.domain_name != ""
    ? "curl https://${var.domain_name}/healthz"
  : "curl -k https://${aws_eip.sandbox.public_ip}/healthz")
}

output "fetch_admin_key_command" {
  description = "Prints the admin key for `airlock-sandbox-keys --admin` (needs ssm:GetParameter on it)"
  value       = "aws ssm get-parameter --region ${var.aws_region} --name ${aws_ssm_parameter.admin_key.name} --with-decryption --query Parameter.Value --output text"
}
