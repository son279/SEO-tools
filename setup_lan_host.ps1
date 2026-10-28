# One-time setup for the PC that hosts the SEO Tools MCP server (crawler + backlinks) on the LAN.
# Run in an *Administrator* PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup_lan_host.ps1 [-BacklinkDataDir D:\backlink_data]
# BacklinkDataDir = folder with clients.txt, host_vertices\, host_edges_only\, backlink_index\ ...
# Default: existing BACKLINK_DATA_DIR, else <this folder>\backlink_data.
param([string]$BacklinkDataDir = "")
$ErrorActionPreference = "Stop"
$seoDir    = $PSScriptRoot
$tokenFile = Join-Path $env:USERPROFILE ".seo_mcp_token"
$port      = 8765

if (-not $BacklinkDataDir) {
    $BacklinkDataDir = if ($env:BACKLINK_DATA_DIR) { $env:BACKLINK_DATA_DIR } else { Join-Path $seoDir "backlink_data" }
}
New-Item -ItemType Directory -Force $BacklinkDataDir | Out-Null
$BacklinkDataDir = (Resolve-Path $BacklinkDataDir).Path

# 1. Shared bearer token (created once)
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [BitConverter]::ToString($bytes).Replace("-", "").ToLower() | Set-Content -NoNewline $tokenFile
}
$token = Get-Content $tokenFile

# 2. Where the backlink data lives (machine-wide so the boot task sees it)
[Environment]::SetEnvironmentVariable("BACKLINK_DATA_DIR", $BacklinkDataDir, "Machine")

# 3. Firewall: allow inbound 8765 from the local subnet only (Private profile)
$name = "SEO MCP $port"
if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $port `
        -RemoteAddress LocalSubnet -Profile Private -Action Allow | Out-Null
}

# 4. Start the server automatically at boot (runs even when nobody is logged in)
$user = "$env:USERDOMAIN\$env:USERNAME"
schtasks /Create /F /TN "SEO MCP tools" /SC ONSTART /RU $user /RP * /RL HIGHEST /TR "`"$seoDir\start_seo_mcp_lan.bat`""

# Clean up the older two-server setup if it exists
$ErrorActionPreference = "Continue"
foreach ($old in "SEO MCP crawler", "SEO MCP backlink") {
    schtasks /Query /TN $old *> $null
    if ($LASTEXITCODE -eq 0) { schtasks /Delete /F /TN $old | Out-Null; Write-Host "Removed old task: $old" }
}
Get-NetFirewallRule -DisplayName "SEO MCP 8766" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$ErrorActionPreference = "Stop"

# 5. Keep the PC awake on AC power
powercfg /change standby-timeout-ac 0

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in "Dhcp","Manual" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Done. Host IP: $ip   (reserve this IP in your router's DHCP settings)"
Write-Host "SEO Tools MCP : http://${ip}:$port/mcp   (crawler + backlink tools)"
Write-Host "Backlink data : $BacklinkDataDir"
Write-Host "Token         : $token   (give this to teammates; stored in $tokenFile)"
Write-Host "Start now without rebooting:  schtasks /Run /TN `"SEO MCP tools`""
