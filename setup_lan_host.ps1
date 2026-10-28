# One-time setup for the PC that hosts both MCP servers on the LAN.
# Run in an *Administrator* PowerShell:  powershell -ExecutionPolicy Bypass -File setup_lan_host.ps1
$ErrorActionPreference = "Stop"
$seoDir      = $PSScriptRoot
$backlinkDir = (Join-Path $env:USERPROFILE "PycharmProjects\PythonProject5")
$tokenFile   = Join-Path $env:USERPROFILE ".seo_mcp_token"

# 1. Shared bearer token (created once, reused by both servers)
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [BitConverter]::ToString($bytes).Replace("-", "").ToLower() | Set-Content -NoNewline $tokenFile
}
$token = Get-Content $tokenFile

# 2. Firewall: allow inbound 8765/8766 from the local subnet only (Private profile)
foreach ($p in 8765, 8766) {
    $name = "SEO MCP $p"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $p `
            -RemoteAddress LocalSubnet -Profile Private -Action Allow | Out-Null
    }
}

# 3. Start both servers automatically at boot (runs even when nobody is logged in)
$user = "$env:USERDOMAIN\$env:USERNAME"
schtasks /Create /F /TN "SEO MCP crawler"  /SC ONSTART /RU $user /RP * /RL HIGHEST /TR "`"$seoDir\start_seo_mcp_lan.bat`""
schtasks /Create /F /TN "SEO MCP backlink" /SC ONSTART /RU $user /RP * /RL HIGHEST /TR "`"$backlinkDir\start_backlink_mcp_lan.bat`""

# 4. Keep the PC awake on AC power
powercfg /change standby-timeout-ac 0

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in "Dhcp","Manual" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Done. Host IP: $ip   (reserve this IP in your router's DHCP settings)"
Write-Host "Crawler : http://${ip}:8765/mcp"
Write-Host "Backlink: http://${ip}:8766/mcp"
Write-Host "Token   : $token   (give this to teammates; stored in $tokenFile)"
Write-Host "Start now without rebooting:  schtasks /Run /TN `"SEO MCP crawler`"  and  schtasks /Run /TN `"SEO MCP backlink`""
