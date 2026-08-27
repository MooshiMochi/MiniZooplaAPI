# manage-keys.ps1 — create / list / revoke / delete Mini Zoopla API keys via the live API.
# Windows PowerShell equivalent of manage-keys.sh.
#
# Reads config from .env in the script's directory (or current dir):
#   MINI_ZOOPLA_ADMIN_KEY   (required) — the admin key that mints others
#   MINI_ZOOPLA_HOST        (default 127.0.0.1)
#   MINI_ZOOPLA_PORT        (default 8000)
#
# Usage (from PowerShell):
#   .\manage-keys.ps1 list
#   .\manage-keys.ps1 create <owner> [rate_limit]
#   .\manage-keys.ps1 revoke <key_id>     # soft delete (kept in DB, deactivated)
#   .\manage-keys.ps1 delete <key_id>     # hard delete (purged from DB)
#   .\manage-keys.ps1 show   <key_id>
#   .\manage-keys.ps1 help [command]
#
# Any command accepts -Help for its own usage.
#
# Examples:
#   .\manage-keys.ps1 create sheets_user 60
#   .\manage-keys.ps1 revoke a1b2c3d4e5f6
#   .\manage-keys.ps1 delete a1b2c3d4e5f6

param(
    [Parameter(Position=0, Mandatory=$false)]
    [ValidateSet("list","create","revoke","delete","show","help","")]
    [string]$Command = "help",
    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$Args,
    [switch]$Help
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

function Usage-Cmd($c) {
    switch ($c) {
        "list"    { "Usage: .\manage-keys.ps1 list" }
        "create"  { "Usage: .\manage-keys.ps1 create <owner> [rate_limit]" }
        "revoke"  { "Usage: .\manage-keys.ps1 revoke <key_id>   (soft delete - key stays in DB, deactivated)" }
        "delete"  { "Usage: .\manage-keys.ps1 delete <key_id>   (hard delete - purged from DB)" }
        "show"    { "Usage: .\manage-keys.ps1 show <key_id>" }
        "help"    { "Usage: .\manage-keys.ps1 help [command]" }
        default   { "Unknown command: $c" }
    }
}

if ($Help) {
    if ($Command -and $Command -ne "help" -and $Command -ne "") { Usage-Cmd $Command } else { Get-Help $MyInvocation.MyCommand.Path }
    exit 0
}

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
        if ($Args.Count -lt 1) { Write-Error (Usage-Cmd "create"); exit 1 }
        $owner = $Args[0]
        $body = @{ owner = $owner }
        if ($Args.Count -ge 2 -and $Args[1]) { $body.rate_limit = [int]$Args[1] }
        $json = $body | ConvertTo-Json -Compress
        Write-Host "Creating key: $json"
        $resp = Invoke-RestMethod -Uri "$Base/admin/keys" -Method Post -ContentType "application/json" -Headers $Headers -Body $json
        $resp | ConvertTo-Json -Depth 10
        Write-Warning "The 'key' value above is shown only once. Store it securely."
    }
    "revoke" {
        if ($Args.Count -lt 1) { Write-Error (Usage-Cmd "revoke"); exit 1 }
        Invoke-RestMethod -Uri "$Base/admin/keys/$($Args[0])" -Method Delete -Headers $Headers | ConvertTo-Json -Depth 10
    }
    "delete" {
        if ($Args.Count -lt 1) { Write-Error (Usage-Cmd "delete"); exit 1 }
        Invoke-RestMethod -Uri "$Base/admin/keys/$($Args[0])?purge=true" -Method Delete -Headers $Headers | ConvertTo-Json -Depth 10
    }
    "show" {
        if ($Args.Count -lt 1) { Write-Error (Usage-Cmd "show"); exit 1 }
        $all = Invoke-RestMethod -Uri "$Base/admin/keys" -Headers $Headers
        $found = $all.keys | Where-Object { $_.key_id -eq $Args[0] }
        if ($found) { $found | ConvertTo-Json -Depth 10 } else { Write-Warning "key_id not found" }
    }
    "help" {
        if ($Args.Count -ge 1) { Usage-Cmd $Args[0] } else { Get-Help $MyInvocation.MyCommand.Path }
    }
    "" {
        Get-Help $MyInvocation.MyCommand.Path
    }
}
