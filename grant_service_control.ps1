# grant_service_control.ps1 — run ONCE as Administrator.
#
# Two fixes for the already-installed trading-agent service:
#
#   1. Unbuffered logging. Python block-buffers stdout/stderr when they are not a
#      TTY, so under NSSM the log files sit at 0 bytes until a 4-8 KB buffer fills,
#      and a hard crash loses whatever was still in it -- precisely the incident
#      where the log matters. Observed live 2026-08-10: healthy cycles, two empty
#      log files.
#
#   2. Delegated start/stop. By default only Administrators may control a service,
#      so every restart needs a human at a UAC prompt and the bot cannot be
#      recovered unattended. This grants RP (start), WP (stop) and DT
#      (pause/continue) on THIS SERVICE ONLY to the current user. It confers no
#      other privilege and touches no other service.
#
# After this, `nssm restart trading-agent` works without elevation.

param([string]$SvcName = "trading-agent-binance")

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Write-Error "Run this script as Administrator."; exit 1
}

# -- 1. unbuffered, UTF-8 logging ---------------------------------------------

& nssm set $SvcName AppEnvironmentExtra `
    "PYTHONUNBUFFERED=1" "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8"
Write-Host "  logging: PYTHONUNBUFFERED + UTF-8 set" -ForegroundColor Green

# -- 2. delegate start/stop to the owning account -----------------------------

$Sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$Sd  = (& sc.exe sdshow $SvcName | Where-Object { $_ -match '^D:' }) -join ''

if (-not $Sd) { Write-Error "could not read security descriptor for $SvcName"; exit 1 }

if ($Sd -match [regex]::Escape($Sid)) {
    Write-Host "  control already granted to $Sid" -ForegroundColor Yellow
} else {
    $New = $Sd + "(A;;CCLCSWRPWPDTLOCRRC;;;$Sid)"
    # Do NOT pipe to Out-Null: sc.exe reports sdset failures on stdout, and
    # swallowing them makes a silent no-op look like success. (That happened on
    # 2026-08-10: the script reported "granted" while the descriptor was unchanged.)
    $out = & sc.exe sdset $SvcName $New 2>&1 | Out-String
    $verify = (& sc.exe sdshow $SvcName | Where-Object { $_ -match '^D:' }) -join ''
    if ($verify -match [regex]::Escape($Sid)) {
        Write-Host "  granted start/stop on $SvcName to $(([Security.Principal.WindowsIdentity]::GetCurrent()).Name)" -ForegroundColor Green
    } else {
        Write-Warning "sdset did not take effect. sc.exe said:`n$out"
        Write-Warning "SD is still: $verify"
    }
}

# -- restart so both take effect ----------------------------------------------

& nssm restart $SvcName
Start-Sleep -Seconds 3
Write-Host ""
Write-Host "  status: $(& nssm status $SvcName)" -ForegroundColor Cyan
Write-Host "  verify (unelevated):  nssm status $SvcName" -ForegroundColor Yellow
Write-Host "  logs:   Get-Content W:\ai-trading-agent\logs\trading-agent-stdout.log -Tail 30 -Wait" -ForegroundColor Yellow
