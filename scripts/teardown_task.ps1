# teardown_task.ps1 -- Remove the WeChat Video Bot scheduled task
# ============================================================================
# Stops any running instance and unregisters the task from Task Scheduler.
#
# Usage (Run as Administrator):
#   powershell -ExecutionPolicy Bypass -File scripts\teardown_task.ps1
#
# Optional:
#   -TaskName <name>    Custom task name (default: DouyinEmailBot)
# ============================================================================
param(
    [string] $TaskName = "DouyinEmailBot"
)

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' not found -- nothing to do."
    exit 0
}

# Stop any running instance (ignores error if none is running)
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Stopped running instance of '$TaskName'."
} catch {
    Write-Host "No running instance to stop."
}

# Remove the task
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed successfully."
Write-Host ""
Write-Host "Log files in logs\bot.log* are preserved -- delete manually if desired."
