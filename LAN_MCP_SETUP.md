# Sharing the SEO Tools MCP server on the office network (LAN)

One machine (the **host**) runs the server, and teammates' Claude Desktop or Claude Code connects to it. A single server exposes both the crawler tools and the backlink tools.

| Server | URL |
|---|---|
| `seo-tools` (crawler + backlinks) | `http://<HOST-IP>:8765/mcp` |

Running `seo_mcp_server.py` with no arguments still uses stdio, so a local Claude Desktop config keeps working.

## Windows host (one time)

1. Make sure Windows marks the office network as **Private** (Settings > Network > Properties).
2. Open **PowerShell as Administrator** in the project folder and run:
   ```
   powershell -ExecutionPolicy Bypass -File setup_lan_host.ps1 -BacklinkDataDir D:\path\to\backlink_data
   ```
   Leave out `-BacklinkDataDir` to use `<project>\backlink_data`. The script:
   - creates a shared token (`%USERPROFILE%\.seo_mcp_token`)
   - sets `BACKLINK_DATA_DIR` machine-wide
   - opens port 8765 to the local subnet only
   - registers the server to start at boot
   - disables sleep on AC power
   - removes the older two-server tasks if they exist

   It prints the IP, URL and token.
3. In your router, give the host a **DHCP reservation** so its IP never changes.
4. Start now: `schtasks /Run /TN "SEO MCP tools"`, or double-click `start_seo_mcp_lan.bat` to run it in a window.

Before anyone can run backlink queries, run the pipeline stages and `build_index` once on the host. In Claude, call `backlink_pipeline_status` and it suggests the next step.

## Teammate machines

These need Node.js (for `npx`). Edit Claude Desktop's config (Settings > Developer > Edit Config), replace `HOST-IP` and `TOKEN`, then restart Claude:

```json
{
  "mcpServers": {
    "seo-tools": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST-IP:8765/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    }
  }
}
```

Claude Code:

```
claude mcp add --transport http seo-tools http://HOST-IP:8765/mcp --header "Authorization: Bearer TOKEN"
```

## Quick check

From a teammate machine, this should print `401` without the token, which means the server is reachable:
`curl -s -o NUL -w "%{http_code}" -X POST http://HOST-IP:8765/mcp` (on macOS/Linux use `-o /dev/null`).

## Notes

- Crawls (`run_seo_crawl`) and backlink jobs (`start_stage`) run **on the host**. Relative `output_dir` values land in the host's project folder, and backlink files land in `BACKLINK_DATA_DIR`.
- Backlink jobs are tracked in memory, so restarting the server forgets job ids. A stage that's already running keeps going, and its log stays in `<BACKLINK_DATA_DIR>/job_logs/`.
- Traffic is plain HTTP inside the LAN. Don't port-forward 8765 to the internet; use a VPN (e.g. Tailscale) or a tunnel with proper auth for outside access.
- To rotate the token, delete `%USERPROFILE%\.seo_mcp_token`, re-run the setup script, and restart the task.

## Hosting on a Mac instead

See [MAC_PIPELINE_SETUP.md](MAC_PIPELINE_SETUP.md). In short:

1. Copy the project to `~/seo-mcp/SEO System`, outside Downloads, Documents and Desktop. From a Windows PC, `package_for_mac.ps1` builds `Desktop\seo-mcp-mac.zip`; add `-IncludeBacklink` to include the backlink query data.
2. Install Python 3.10+, then run `bash setup_mac_host.sh` (or `BACKLINK_DATA_DIR=/path bash setup_mac_host.sh`).
3. Check: `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/mcp` should print 401.

Logs: `~/Library/Logs/seo-mcp/seo-tools.log`. Restart: `sudo launchctl kickstart -k system/com.seotools.mcp`.
