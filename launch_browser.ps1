# launch_browser.ps1 -- Open Douyin File Browser with connectivity check
# ============================================================================
# Double-click launch_browser.bat to run this script.  It will:
#   1. Check that the Ubuntu server is reachable on the LAN
#   2. Check that the file browser port is open
#   3. Open your default browser if everything is OK
#   4. Show a clear error message (in Chinese) if something is wrong
# ============================================================================

param(
    [string] $Server = "192.168.0.103",
    [int]    $Port   = 8081
)

$ErrorActionPreference = "Stop"
$URL = "http://${Server}:${Port}"

function Write-Status($msg)  { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Good($msg)    { Write-Host "  OK $msg" -ForegroundColor Green }
function Write-Warn($msg)    { Write-Host "  !! $msg" -ForegroundColor Yellow }
function Write-Bad($msg)     { Write-Host "  XX $msg" -ForegroundColor Red }
function Write-Title($msg)   { Write-Host ""; Write-Host $msg -ForegroundColor Cyan }

Clear-Host
Write-Title "Douyin File Browser - Connection Check"
Write-Host "Server: ${Server}:${Port}"
Write-Host ""

# Step 1: ICMP ping
Write-Status "Checking if server is online..."

$ping = Test-Connection -ComputerName $Server -Count 1 -Quiet -ErrorAction SilentlyContinue
if (-not $ping) {
    Clear-Host
    Write-Title "ERROR: Server Unreachable"
    Write-Host ""
    Write-Bad "Cannot ping ${Server}"
    Write-Host ""
    Write-Host "Possible causes:"
    Write-Host "  - Ubuntu server is off or sleeping"
    Write-Host "  - Wi-Fi disconnected (interface wlp6s0)"
    Write-Host "  - IP address changed - check with: ip addr show wlp6s0"
    Write-Host "  - Windows and server not on same LAN"
    Write-Host ""
    Write-Host "Troubleshooting:"
    Write-Host "  1. Check Ubuntu is powered on"
    Write-Host "  2. Test SSH: ssh nouveau@${Server}"
    Write-Host "  3. Check IP on server: ip addr show wlp6s0"
    exit 1
}
Write-Good "Server online"

# Step 2: TCP port check
Write-Status "Checking file browser port (${Port})..."

$tcp = Test-NetConnection -ComputerName $Server -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
if (-not $tcp) {
    Clear-Host
    Write-Title "ERROR: Port Not Open"
    Write-Host ""
    Write-Warn "Server ${Server} is online, but port ${Port} is closed"
    Write-Host ""
    Write-Host "Possible causes:"
    Write-Host "  - file_browser container is not running"
    Write-Host "  - Docker service stopped"
    Write-Host "  - Port mapping misconfigured"
    Write-Host ""
    Write-Host "Troubleshooting (SSH to server first):"
    Write-Host "  1. ssh nouveau@${Server}"
    Write-Host "  2. Check containers: sudo docker compose ps"
    Write-Host "  3. View logs: sudo docker logs douyin_file_browser --tail 20"
    Write-Host "  4. Restart: cd ~/douyin_email_bot"
    Write-Host "     then: sudo docker compose up -d file_browser"
    exit 1
}
Write-Good "File browser port ${Port} open"

# Step 3: HTTP check
Write-Status "Checking web service response..."

try {
    $response = Invoke-WebRequest -Uri $URL -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    if ($response.StatusCode -eq 200) {
        Write-Good "Web service responding (HTTP 200)"
    } else {
        Write-Warn "Web service returned HTTP $($response.StatusCode)"
    }
} catch {
    Clear-Host
    Write-Title "ERROR: No HTTP Response"
    Write-Host ""
    Write-Warn "Port ${Port} accepts connections but returns no valid HTTP response"
    Write-Host ""
    Write-Host "Possible causes:"
    Write-Host "  - Flask app crashed or failed to start"
    Write-Host "  - File mount error inside container"
    Write-Host ""
    Write-Host "Troubleshooting:"
    Write-Host "  1. ssh nouveau@${Server}"
    Write-Host "  2. View container logs: sudo docker logs douyin_file_browser --tail 30"
    Write-Host "  3. Restart container: sudo docker compose restart file_browser"
    exit 1
}

# All checks passed
Clear-Host
Write-Title "All Checks Passed - Opening Browser"
Write-Host ""
Write-Good "Server:   ${Server}"
Write-Good "Port:     ${Port}"
Write-Good "URL:      ${URL}"
Write-Host ""
Write-Status "Launching default browser..."

Start-Process $URL

Write-Good "Browser launched!"
Write-Host ""
Write-Host "Tip: If the page shows an error, check server logs:"
Write-Host "  ssh nouveau@${Server} 'sudo docker logs douyin_file_browser --tail 20'"
