# Bundles everything a new Mac host needs into Desktop\seo-mcp-mac.zip
# Run in normal PowerShell:  powershell -ExecutionPolicy Bypass -File package_for_mac.ps1
# Crawler only by default. Add -IncludeBacklink to also bundle PythonProject5 + its data (~2 GB).
param([switch]$IncludeBacklink)
$ErrorActionPreference = "Stop"
$seo   = $PSScriptRoot
$bl    = (Join-Path $env:USERPROFILE "PycharmProjects\PythonProject5")
$root  = Join-Path $env:TEMP "seo-mcp-pack"
$stage = Join-Path $root "seo-mcp"
$out   = Join-Path ([Environment]::GetFolderPath("Desktop")) "seo-mcp-mac.zip"

if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Force "$stage\SEO System" | Out-Null
if ($IncludeBacklink) { New-Item -ItemType Directory -Force "$stage\PythonProject5\backlink_index" | Out-Null }

Write-Host "Copying SEO System (code only, no Windows venv / old crawl outputs)..."
robocopy $seo "$stage\SEO System" /E /XD .venv .venv-mac .git .idea __pycache__ build dist seo_output seo_output_* /XF *.rar *.bak /NFL /NDL /NJH /NJS | Out-Null

if ($IncludeBacklink) {
    Write-Host "Copying backlink server code..."
    robocopy $bl "$stage\PythonProject5" *.py *.sh clients.txt /NFL /NDL /NJH /NJS | Out-Null
    robocopy "$bl\mcp_server" "$stage\PythonProject5\mcp_server" /E /XD __pycache__ job_logs /XF *.bak /NFL /NDL /NJH /NJS | Out-Null

    Write-Host "Copying backlink data (this is the big part)..."
    foreach ($f in "backlink_edges.csv", "generation_summary.csv", "follow_status_summary.csv",
                   "common_crawl_host_ranks.csv", "query.sqlite3") {
        $src = Join-Path "$bl\backlink_index" $f
        if (Test-Path $src) { Copy-Item $src "$stage\PythonProject5\backlink_index\"; Write-Host "  + $f" }
    }
}

Write-Host "Zipping to $out ..."
if (Test-Path $out) { Remove-Item $out -Force }
tar.exe -a -c -f $out -C $root seo-mcp
Remove-Item $root -Recurse -Force
$gb = [math]::Round((Get-Item $out).Length / 1GB, 2)
Write-Host "Done: $out ($gb GB). Copy it to the new Mac."
