# launcher.ps1 -- Invoked by Task Scheduler to run the WeChat Video Bot
# ============================================================================
# This script finds uv.exe, sets the working directory to the project root,
# and launches the bot.  All logging goes to logs/bot.log via Python's own
# RotatingFileHandler -- nothing is written to stdout/stderr here.
# ============================================================================
$ErrorActionPreference = "Stop"

# Project root is the parent of the scripts/ directory
$projectDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $projectDir

# ── Locate uv.exe ────────────────────────────────────────────────────────
# Task Scheduler may not have the user's full PATH.  Try known install
# locations first, then fall back to Get-Command.
$uvExe = $null
$candidates = @(
    "$env:USERPROFILE\.local\bin\uv.exe",
    "$env:USERPROFILE\.cargo\bin\uv.exe",
    "$env:LOCALAPPDATA\Programs\uv\uv.exe"
)
foreach ($c in $candidates) {
    if (Test-Path -Path $c) { $uvExe = $c; break }
}
if (-not $uvExe) {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { $uvExe = $found.Source }
}
if (-not $uvExe) {
    Write-Error "uv.exe not found. Checked: $($candidates -join ', '), PATH"
    exit 1
}

# ── Run the bot ──────────────────────────────────────────────────────────
# All output (stdout + stderr) is captured by the bot's own file logging.
# Any uncaught crash output ends up in the Task Scheduler operational log
# (visible in Event Viewer).
& $uvExe run python main.py
exit $LASTEXITCODE
