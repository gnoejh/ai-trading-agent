# install_nssm_service.ps1 — Install the trading-agent service via NSSM.
#
# ONE service, every venue: Binance trades 24/7, Kiwoom KR/US run
# measurement-only cycles in their own sessions. Installing also removes the
# legacy per-broker service (trading-agent-binance) if present.
#
#   .\install_nssm_service.ps1                      # trading-agent
#   .\install_nssm_service.ps1 -Uninstall
#
# Each service trades its own venue and serves the Telegram control surface only
# if it is `notify.telegram.command_owner` — one getUpdates consumer per token.
#
# Prerequisites:
#   1. Install NSSM:  winget install nssm  (or https://nssm.cc)
#   2. Run this script as Administrator
#
# After install:
#   nssm start trading-agent-binance
#   nssm status trading-agent-binance
#   nssm restart trading-agent-binance
#   nssm stop trading-agent-binance
#
# NOTE: the service inherits config.yaml as it stands at start. Check
#       `uv run python -m trading.preflight` first — it prints whether the run
#       will be live and how each risk limit sizes against the account.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$Root    = "W:\ai-trading-agent"
$LogDir  = "$Root\logs"
$UvExe   = "$env:USERPROFILE\.local\bin\uv.exe"
$SvcName = "trading-agent"
$LegacySvc = "trading-agent-binance"

# -- Verify prerequisites -----------------------------------------------------

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Write-Error "Run this script as Administrator."; exit 1
}

$NssmCmd = Get-Command nssm -ErrorAction SilentlyContinue
$NssmExe = if ($NssmCmd) { $NssmCmd.Source } else { $null }
if (-not $NssmExe) {
    Write-Error "nssm not found. Install with: winget install nssm"; exit 1
}
if (-not (Test-Path $UvExe)) {
    Write-Error "uv.exe not found at $UvExe"; exit 1
}
if (-not (Test-Path $Root)) {
    Write-Error "Repo root not found at $Root"; exit 1
}

# -- Uninstall path -----------------------------------------------------------

if ($Uninstall) {
    Write-Host "Removing $SvcName..." -ForegroundColor Cyan
    & $NssmExe stop   $SvcName 2>$null
    & $NssmExe remove $SvcName confirm
    Write-Host "$SvcName removed." -ForegroundColor Green
    exit 0
}

# -- Remove old service if present (re-install support) -----------------------

try {
    $legacy = & $NssmExe status $LegacySvc 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removing legacy service: $LegacySvc" -ForegroundColor Yellow
        & $NssmExe stop   $LegacySvc 2>&1 | Out-Null
        & $NssmExe remove $LegacySvc confirm
    }
} catch { }

try {
    $status = & $NssmExe status $SvcName 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removing existing service: $SvcName" -ForegroundColor Yellow
        & $NssmExe stop   $SvcName 2>&1 | Out-Null
        & $NssmExe remove $SvcName confirm
    }
} catch { }

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

# -- Install ------------------------------------------------------------------

Write-Host "Installing $SvcName..." -ForegroundColor Cyan

& $NssmExe install $SvcName $UvExe
& $NssmExe set     $SvcName AppParameters   "run python run_service.py"
& $NssmExe set     $SvcName AppDirectory    $Root
& $NssmExe set     $SvcName DisplayName     "AI Trading Agent"
& $NssmExe set     $SvcName Description     "Autonomous trading agent: DeepSeek decisions, cost-derived exits; Binance 24/7 + Kiwoom KR/US measurement"
& $NssmExe set     $SvcName AppStdout       "$LogDir\$SvcName-stdout.log"
& $NssmExe set     $SvcName AppStderr       "$LogDir\$SvcName-stderr.log"
& $NssmExe set     $SvcName AppRotateFiles  1
& $NssmExe set     $SvcName AppRotateBytes  10485760    # 10 MB
& $NssmExe set     $SvcName Start           SERVICE_AUTO_START
& $NssmExe set     $SvcName AppRestartDelay 5000        # 5 s before restart on crash
& $NssmExe set     $SvcName AppThrottle     10000       # 10 s min between restarts

# PYTHONUNBUFFERED is not optional for a service. Python block-buffers stdout and
# stderr when they are not a TTY, so under NSSM the log files sit at 0 bytes until
# a 4-8 KB buffer fills -- and a hard crash loses whatever was still in it. That is
# exactly the incident where the log matters. (Observed live 2026-08-10: this
# service ran healthy cycles with two empty log files.)
#
# PYTHONUTF8 because the console here is cp949 and the whole corpus is Korean;
# without it every Korean broker message logs as mojibake.
& $NssmExe set     $SvcName AppEnvironmentExtra `
    "PYTHONUNBUFFERED=1" "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8"

# NSSM's own restart only covers the app under the wrapper. If nssm.exe itself is
# killed, the service stays STOPPED forever without SCM-level recovery.
sc.exe failure $SvcName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null

# -- Delegate start/stop to the owning account --------------------------------
#
# By default only Administrators may control a service, so every restart needs a
# UAC prompt -- which means an unattended operator (or an agent session) cannot
# recover the bot without a human at the keyboard. Grant RP (start), WP (stop)
# and DT (pause/continue) on THIS SERVICE ONLY to the installing user. Scope is
# one service; it confers no other privilege.
$Sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$Sd  = (& sc.exe sdshow $SvcName | Where-Object { $_ -match '^D:' }) -join ''
if ($Sd -and $Sd -notmatch [regex]::Escape($Sid)) {
    $Sd = $Sd -replace '(?=\(A;;.*;;;SY\))', ''   # keep existing ACEs untouched
    $New = $Sd + "(A;;CCLCSWRPWPDTLOCRRC;;;$Sid)"
    & sc.exe sdset $SvcName $New | Out-Null
    Write-Host "  granted start/stop on $SvcName to $((
        [Security.Principal.WindowsIdentity]::GetCurrent()).Name)" -ForegroundColor Green
}

Write-Host ""
Write-Host "  $SvcName installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Preflight:  uv run python -m trading.preflight" -ForegroundColor Yellow
Write-Host "  Start:      nssm start $SvcName" -ForegroundColor Yellow
Write-Host "  Status:     nssm status $SvcName" -ForegroundColor Yellow
Write-Host "  Stop:       nssm stop $SvcName" -ForegroundColor Yellow
Write-Host "  Logs:       Get-Content $LogDir\$SvcName-stdout.log -Tail 50 -Wait" -ForegroundColor Yellow
Write-Host "  Halt now:   New-Item -ItemType File data\HALT   (blocks entries; exits still run)" -ForegroundColor Yellow
Write-Host "  Uninstall:  .\install_nssm_service.ps1 -Uninstall" -ForegroundColor Yellow
Write-Host ""
