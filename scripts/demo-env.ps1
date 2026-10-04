# Points this PowerShell window at the deployed sandbox API, for examples\demo_live.py,
# scripts\smoke_test.py and the Python client. Run it "dot-sourced" so the variables stay set:
#
#     . .\scripts\demo-env.ps1              # tenant "default"
#     . .\scripts\demo-env.ps1 -Tenant acme
#     . .\scripts\demo-env.ps1 -Admin        # also loads SANDBOX_ADMIN_KEY (airlock-sandbox-keys --admin)
#
# Reads the URL from Terraform outputs and the tenant's key from SSM (needs the
# AWS CLI logged in to the account that ran terraform apply). The key is never printed.
param([string]$Tenant = "default", [switch]$Admin)

$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..\terraform")
try {
    $url = terraform output -raw api_endpoint
    $pairs = Invoke-Expression (terraform output -raw fetch_api_keys_command)
    if ($Admin) { $adminKey = Invoke-Expression (terraform output -raw fetch_admin_key_command) }
} finally {
    Pop-Location
}

$key = $null
foreach ($pair in ($pairs -split ",")) {
    $name, $value = $pair -split ":", 2
    if ($name -eq $Tenant) { $key = $value }
}
if (-not $key) {
    throw "No API key for tenant '$Tenant'. Tenants in this deployment: $((($pairs -split ',') | ForEach-Object { ($_ -split ':')[0] }) -join ', ')"
}

$env:SANDBOX_API_URL = $url
$env:SANDBOX_API_KEY = $key
# A bare IP means no domain was configured, so Caddy serves a self-signed certificate.
if ($url -match '^https://\d+\.\d+\.\d+\.\d+') { $env:SANDBOX_API_INSECURE = "1" } else { Remove-Item Env:SANDBOX_API_INSECURE -ErrorAction SilentlyContinue }

Write-Host "Sandbox API : $url"
Write-Host "Tenant      : $Tenant (key loaded, $($key.Length) chars)"
Write-Host "Self-signed : $([bool]$env:SANDBOX_API_INSECURE)"
if ($Admin) {
    $env:SANDBOX_ADMIN_KEY = $adminKey
    Write-Host "Admin key   : loaded ($($adminKey.Length) chars) - airlock-sandbox-keys --admin list"
}
Write-Host ""
Write-Host "Next: .venv\Scripts\python examples\demo_live.py   (or --skip-agent / --act 1..4)"
