# manage-keys.ps1 — create / list / revoke Mini Zoopla API keys via the live API.
# Windows PowerShell equivalent of manage-keys.sh.
#
# Reads config from .env in the script's directory (or current dir):
#   MINI_ZOOPLA_ADMIN_KEY   (required) — the admin key that mints others
#   MINI_ZOOPLA_HOST        (default 127.0.0.1)
#   MINI_ZOOPLA_PORT        (default 8000)
#
# Usage (from PowerShell):
#   .\manage-keys.ps1 list
#   .\manage-keys.ps1 create <owner> [allowed_branches_csv] [rate_limit]
#   .\manage-keys.ps1 revoke <key_id>
#   .\manage-keys.ps1 show   <key_id>
#
# Examples:
#   .\manage-keys.ps1 create sheets_user 56042,12345 60
#   .\manage-keys.ps1 revoke a1b2c3d4e5f6

param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("list","create","revoke","show","help")]
    [string]$Command,
    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$Args
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$EnvFile = Join-Path $ScriptDir ".env"
if (-not (Test-Path $EnvFile)) { $EnvFile = ".\.env" }

# Minimal .env loader (real env vars win).
if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $k, $v = $line.Split("=", 2)
        $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'").Trim()
        if (-not (Get-Item "Env:$k" -ErrorAction SilentlyContinue)) { Set-Item "Env:$k" $v }
    }
}

$AdminKey = $env:MINI_ZOOPLA_ADMIN_KEY
$Host_ = if ($env:MINI_ZOOPLA_HOST) { $env:MINI_ZOOPLA_HOST } else { "127.0.0.1" }
$Port = if ($env:MINI_ZOOPLA_PORT) { $env:MINI_ZOOPLA_PORT } else { "8000" }
$Base = "http://${Host_}:${Port}"

if (-not $AdminKey) {
    Write-Error "ERROR: MINI_ZOOPLA_ADMIN_KEY is not set. Set it in your .env file or `$env:MINI_ZOOPLA_ADMIN_KEY."
    exit 1
}

$Headers = @{ "X-Admin-Key" = $AdminKey }

switch ($Command) {
    "list" {
        (Invoke-RestMethod -Uri "$Base/admin/keys" -Headers $Headers) | ConvertTo-Json -Depth 10
    }
    "create" {
        if ($Args.Count -lt 1) { Write-Error "Usage: .\manage-keys.ps1 create <owner> [allowed_branches_csv] [rate_limit]"; exit 1 }
        $owner = $Args[0]
        $body = @{ owner = $owner }
        if ($Args.Count -ge 2 -and $Args[1]) {
            $body.allowed_branches = $Args[1].Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        }
        if ($Args.Count -ge 3 -and $Args[2]) { $body.rate_limit = [int]$Args[2] }
        $json = $body | ConvertTo-Json -Compress
        Write-Host "Creating key: $json"
        $resp = Invoke-RestMethod -Uri "$Base/admin/keys" -Method Post -ContentType "application/json" -Headers $Headers -Body $json
        $resp | ConvertTo-Json -Depth 10
        Write-Warning "The 'key' value above is shown only once. Store it securely."
    }
    "revoke" {
        if ($Args.Count -lt 1) { Write-Error "Usage: .\manage-keys.ps1 revoke <key_id>"; exit 1 }
        Invoke-RestMethod -Uri "$Base/admin/keys/$($Args[0])" -Method Delete -Headers $Headers | ConvertTo-Json -Depth 10
    }
    "show" {
        if ($Args.Count -lt 1) { Write-Error "Usage: .\manage-keys.ps1 show <key_id>"; exit 1 }
        $all = Invoke-RestMethod -Uri "$Base/admin/keys" -Headers $Headers
        $found = $all.keys | Where-Object { $_.key_id -eq $Args[0] }
        if ($found) { $found | ConvertTo-Json -Depth 10 } else { Write-Warning "key_id not found" }
    }
    "help" {
        Get-Help $MyInvocation.MyCommand.Path
    }
}
