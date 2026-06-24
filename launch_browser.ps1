# launch_browser.ps1 — Open Douyin File Browser with connectivity check
# ============================================================================
# Double-click this script or run from terminal.  It will:
#   1. Check that the Ubuntu server is reachable on the LAN
#   2. Check that the file browser port is open
#   3. Open your default browser if everything is OK
#   4. Show a clear error message (in Chinese) if something is wrong
#
# Usage:
#   powershell -File launch_browser.ps1
#   (double-click in Explorer also works — shows a window that stays open)
# ============================================================================

param(
    [string] $Server = "192.168.0.103",
    [int]    $Port   = 8081
)

$ErrorActionPreference = "Stop"
$URL = "http://${Server}:${Port}"

# ── Helper: colored output without requiring colorama ─────────────────
function Write-Status($msg)  { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Good($msg)    { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg)    { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Bad($msg)     { Write-Host "  ✗ $msg" -ForegroundColor Red }
function Write-Title($msg)   { Write-Host ""; Write-Host $msg -ForegroundColor Cyan }

# ── Main ──────────────────────────────────────────────────────────────

Clear-Host
Write-Title "📦 Douyin 下载浏览 — 连接检测"
Write-Host "服务器: ${Server}:${Port}"
Write-Host ""

# Step 1: ICMP ping (fastest — tells us if host is on the LAN)
Write-Status "检测服务器是否在线..."

$ping = Test-Connection -ComputerName $Server -Count 1 -Quiet -ErrorAction SilentlyContinue
if (-not $ping) {
    Clear-Host
    Write-Title "❌ 无法连接 — 服务器不在线"
    Write-Host ""
    Write-Bad "无法 ping 通 ${Server}"
    Write-Host ""
    Write-Host "可能的原因："
    Write-Host "  • Ubuntu 服务器未开机或已休眠"
    Write-Host "  • 服务器 Wi-Fi 已断开 (wlp6s0)"
    Write-Host "  • IP 地址已变更 — 在服务器上运行: ip addr show wlp6s0"
    Write-Host "  • Windows 与服务器不在同一局域网"
    Write-Host ""
    Write-Host "排查步骤："
    Write-Host "  1. 确认 Ubuntu 服务器电源开启"
    Write-Host "  2. SSH 测试: ssh nouveau@${Server}"
    Write-Host "  3. 在服务器上检查 IP: ip addr show wlp6s0"
    Write-Host ""
    Write-Host "按任意键退出..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
Write-Good "服务器在线"

# Step 2: TCP port check (ensures the file browser container is running)
Write-Status "检测文件浏览服务 (端口 ${Port})..."

$tcp = Test-NetConnection -ComputerName $Server -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
if (-not $tcp) {
    Clear-Host
    Write-Title "⚠️ 服务器在线，但文件浏览服务未启动"
    Write-Host ""
    Write-Warn "服务器 ${Server} 可以 ping 通，但端口 ${Port} 未开放"
    Write-Host ""
    Write-Host "可能的原因："
    Write-Host "  • file_browser 容器未运行"
    Write-Host "  • Docker 服务未启动"
    Write-Host "  • 端口映射配置错误"
    Write-Host ""
    Write-Host "排查步骤："
    Write-Host "  1. SSH 到服务器: ssh nouveau@${Server}"
    Write-Host "  2. 检查容器状态: sudo docker compose ps"
    Write-Host "  3. 查看日志:      sudo docker logs douyin_file_browser --tail 20"
    Write-Host "  4. 重启服务:      cd ~/douyin_email_bot && sudo docker compose up -d file_browser"
    Write-Host ""
    Write-Host "按任意键退出..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
Write-Good "文件浏览服务就绪 (端口 ${Port} 已开放)"

# Step 3: HTTP check (ensure the Flask app is actually responding)
Write-Status "检测 Web 服务响应..."

try {
    $response = Invoke-WebRequest -Uri $URL -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    if ($response.StatusCode -eq 200) {
        Write-Good "Web 服务响应正常 (HTTP 200)"
    } else {
        Write-Warn "Web 服务返回 HTTP $($response.StatusCode)"
    }
} catch {
    Clear-Host
    Write-Title "⚠️ 端口开放但 Web 服务无响应"
    Write-Host ""
    Write-Warn "端口 ${Port} 接受连接，但未返回有效的 HTTP 响应"
    Write-Host ""
    Write-Host "可能的原因："
    Write-Host "  • Flask 应用崩溃或启动失败"
    Write-Host "  • 容器内文件挂载错误"
    Write-Host ""
    Write-Host "排查步骤："
    Write-Host "  1. SSH: ssh nouveau@${Server}"
    Write-Host "  2. 查看容器日志: sudo docker logs douyin_file_browser --tail 30"
    Write-Host "  3. 重启容器:     sudo docker compose restart file_browser"
    Write-Host ""
    Write-Host "按任意键退出..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

# ── All checks passed ─────────────────────────────────────────────────
Clear-Host
Write-Title "✅ 一切正常 — 正在打开浏览器"
Write-Host ""
Write-Good "服务器:   ${Server}"
Write-Good "服务端口: ${Port}"
Write-Good "地址:     ${URL}"
Write-Host ""
Write-Status "正在启动默认浏览器..."

Start-Process $URL

Write-Good "浏览器已打开！"
Write-Host ""
Write-Host "提示: 如果页面显示空白或错误，"
Write-Host "      请查看服务器日志: ssh nouveau@${Server} 'sudo docker logs douyin_file_browser --tail 20'"
Write-Host ""
Start-Sleep -Seconds 2
