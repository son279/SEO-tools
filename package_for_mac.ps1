# Bundles everything a new Mac host needs into Desktop\seo-mcp-mac.zip
# Run in normal PowerShell:  powershell -ExecutionPolicy Bypass -File package_for_mac.ps1
# Code only by default. Add -IncludeBacklink to also bundle the backlink query data
# (clients.txt + the backlink_index CSVs / query DB, a few GB) into "SEO System\backlink_data".
# The raw Common Crawl inputs (host_vertices\, host_edges_only\, tens of GB) are never bundled.
param([switch]$IncludeBacklink)
$ErrorActionPreference = "Stop"
$seo   = $PSScriptRoot
$bl    = if ($env:BACKLINK_DATA_DIR) { $env:BACKLINK_DATA_DIR } else { Join-Path $seo "backlink_data" }
$root  = Join-Path $env:TEMP "seo-mcp-pack"
$stage = Join-Path $root "seo-mcp"
$out   = Join-Path ([Environment]::GetFolderPath("Desktop")) "seo-mcp-mac.zip"

if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Force "$stage\SEO System" | Out-Null

Write-Host "Copying SEO System (code only, no Windows venv / crawl outputs / backlink data)..."
robocopy $seo "$stage\SEO System" /E /XD .venv .venv-mac .git .idea __pycache__ build dist seo_output seo_output_* backlink_data job_logs /XF *.rar *.bak /NFL /NDL /NJH /NJS | Out-Null

if ($IncludeBacklink) {
    $dst = "$stage\SEO System\backlink_data"
    New-Item -ItemType Directory -Force "$dst\backlink_index" | Out-Null
    Write-Host "Copying backlink data from $bl ..."
    if (Test-Path "$bl\clients.txt") { Copy-Item "$bl\clients.txt" $dst }
    foreach ($f in "backlink_edges.csv", "generation_summary.csv", "followup_summary.csv",
                   "followup_edges_classified.csv", "visual_graph_nodes.csv", "visual_graph_edges.csv",
                   "visual_graph.gexf", "clients_backlink_counts.csv", "gen1_deep_summary.csv",
                   "common_crawl_host_ranks.csv", "query.sqlite3") {
        $src = Join-Path "$bl\backlink_index" $f
        if (Test-Path $src) { Copy-Item $src "$dst\backlink_index\"; Write-Host "  + $f" }
    }
}

Write-Host "Zipping to $out ..."
if (Test-Path $out) { Remove-Item $out -Force }
tar.exe -a -c -f $out -C $root seo-mcp
Remove-Item $root -Recurse -Force
$gb = [math]::Round((Get-Item $out).Length / 1GB, 2)
Write-Host "Done: $out ($gb GB). Copy it to the new Mac."
