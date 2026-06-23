# setup_task.ps1 — Install Douyin Email Bot as a hidden scheduled task
# ============================================================================
# Creates a Task Scheduler task that runs the bot at system startup, with
# zero terminal visibility (hidden window).  The bot auto-restarts on crash.
#
# Usage (Run as Administrator):
#   powershell -ExecutionPolicy Bypass -File scripts\setup_task.ps1
#
# Optional switches:
#   -Password           Use password-based logon (runs before login, prompts
#                       for credentials).  Default is Interactive (no
#                       password, runs when the user is logged in).
#   -StartNow           Start the task immediately after registration.
#   -TaskName <name>    Custom task name (default: DouyinEmailBot).
# ============================================================================
param(
    [switch] $Password,
    [switch] $StartNow,
    [string] $TaskName = "DouyinEmailBot"
)

$ErrorActionPreference = "Stop"

# ── Discover project root ────────────────────────────────────────────────
$projectDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$launcherPath = "$projectDir\scripts\launcher.ps1"
$logDir = "$projectDir\logs"

Write-Host "============================================"
Write-Host " Douyin Email Bot — Task Scheduler Setup"
Write-Host "============================================"
Write-Host "  Project : $projectDir"
Write-Host "  Task    : $TaskName"
Write-Host ""

# ── Validation ───────────────────────────────────────────────────────────
$errors = @()

if (-not (Test-Path "$projectDir\.venv")) {
    $errors += ".venv not found — run 'uv sync' first"
}
if (-not (Test-Path "$projectDir\.env")) {
    $errors += ".env file not found — copy .env.example to .env and fill in secrets"
}
if (-not (Test-Path $launcherPath)) {
    $errors += "launcher.ps1 not found at $launcherPath"
}

# Detect uv.exe
$uvExe = $null
@("$env:USERPROFILE\.local\bin\uv.exe",
  "$env:USERPROFILE\.cargo\bin\uv.exe",
  "$env:LOCALAPPDATA\Programs\uv\uv.exe") | ForEach-Object {
    if (-not $uvExe -and (Test-Path $_)) { $uvExe = $_ }
}
if (-not $uvExe) {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { $uvExe = $found.Source }
}
if (-not $uvExe) {
    $errors += "uv.exe not found. Install uv: https://docs.astral.sh/uv/"
} else {
    Write-Host "  uv      : $uvExe"
}

if ($errors.Count -gt 0) {
    Write-Host ""
    Write-Host "ERRORS:" -ForegroundColor Red
    $errors | ForEach-Object { Write-Host "  X $_" -ForegroundColor Red }
    Write-Host ""
    exit 1
}

Write-Host ""

# ── Remove existing task (if any) ────────────────────────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName'..."
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ── Build task components ─────────────────────────────────────────────────
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`"" `
    -WorkingDirectory $projectDir

# Triggers: startup + logon (both with random delay)
$trigger1 = New-ScheduledTaskTrigger -AtStartup -RandomDelay (New-TimeSpan -Seconds 60)
$trigger2 = New-ScheduledTaskTrigger -AtLogon -RandomDelay (New-TimeSpan -Seconds 30)

# Principal
if ($Password) {
    Write-Host "  Logon   : Password (runs before login)"
    $cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Enter your Windows password for the scheduled task"
    $principal = New-ScheduledTaskPrincipal `
        -UserId $cred.UserName `
        -LogonType Password `
        -RunLevel Highest
} else {
    Write-Host "  Logon   : Interactive (no password, runs when user is logged in)"
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Highest
}

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -Hidden `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# ── Register ─────────────────────────────────────────────────────────────
Write-Host "Registering task..."
$regArgs = @{
    TaskName    = $TaskName
    TaskPath    = "\"
    Action      = $action
    Trigger     = $trigger1, $trigger2
    Principal   = $principal
    Settings    = $settings
    Description = "Douyin Email Bot — polls QQ email and downloads Douyin videos/slideshows"
    Force       = $true
}

if ($Password) {
    Register-ScheduledTask @regArgs -User $cred.UserName -Password $cred.GetNetworkCredential().Password
} else {
    Register-ScheduledTask @regArgs
}

Write-Host ""
Write-Host "============================================"
Write-Host " Task '$TaskName' registered successfully!"
Write-Host "============================================"
Write-Host "  Triggers : At system startup (+ up to 60s)"
Write-Host "             At user logon (+ up to 30s)"
Write-Host "  Restart  : 5 retries, 1-minute interval on failure"
Write-Host "  Window   : Hidden (no terminal ever appears)"
Write-Host "  Log file : $logDir\bot.log"
Write-Host ""

if ($StartNow) {
    Write-Host "Starting task immediately..."
    Start-ScheduledTask -TaskName $TaskName
    Write-Host ""
}

Write-Host "Manual commands:"
Write-Host "  Start   : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Stop    : Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host "  Status  : Get-ScheduledTask   -TaskName '$TaskName' | Format-List State, LastRunTime, LastTaskResult"
Write-Host "  Logs    : Get-Content $logDir\bot.log -Tail 50"
Write-Host "  Remove  : .\scripts\teardown_task.ps1"
Write-Host ""
