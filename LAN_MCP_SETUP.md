# Sharing the SEO MCP servers on the office network (LAN)

One PC (the **host**) runs both servers; teammates' Claude Desktop / Claude Code connect to it.

| Server | Folder | LAN URL |
|---|---|---|
| seo-crawler | `SEO System` | `http://<HOST-IP>:8765/mcp` |
| backlink-graph | `PythonProject5` | `http://<HOST-IP>:8766/mcp` |

Running either server with no arguments still uses stdio, so the host's existing Claude Desktop config keeps working.

## Host PC (one time)

1. Make sure Windows marks the office network as **Private** (Settings > Network > Properties).
2. Open **PowerShell as Administrator** in the `SEO System` folder and run:
   `powershell -ExecutionPolicy Bypass -File setup_lan_host.ps1`
   This creates a shared token (`%USERPROFILE%\.seo_mcp_token`), opens ports 8765/8766 to the local subnet only,
   registers both servers to start at boot, and disables sleep on AC power. It prints the IP, URLs and token.
3. In your router, give the host a **DHCP reservation** so its IP never changes.
4. Start now: `schtasks /Run /TN "SEO MCP crawler"` and `schtasks /Run /TN "SEO MCP backlink"`
   (or just double-click the two `start_*_mcp_lan.bat` files to run them in a window).

Before teammates can query backlinks, run the `build_index` tool once on the host (it creates `backlink_index/query.sqlite3`).

## Teammate PCs

Requires Node.js (for `npx`). Edit Claude Desktop's config
(Settings > Developer > Edit Config, i.e. `%APPDATA%\Claude\claude_desktop_config.json`), replace `HOST-IP` and `TOKEN`, then restart Claude:

```json
{
  "mcpServers": {
    "seo-crawler": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST-IP:8765/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    },
    "backlink-graph": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST-IP:8766/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    }
  }
}
```

Claude Code:

```
claude mcp add --transport http seo-crawler    http://HOST-IP:8765/mcp --header "Authorization: Bearer TOKEN"
claude mcp add --transport http backlink-graph http://HOST-IP:8766/mcp --header "Authorization: Bearer TOKEN"
```

## Quick check

From a teammate PC (should print 401 without the token, i.e. the server is reachable):
`curl -s -o NUL -w "%{http_code}" -X POST http://HOST-IP:8765/mcp`

## Notes

- `run_seo_crawl` output folders and backlink `start_stage` jobs run **on the host**. Relative `output_dir` values land in the host's `SEO System` folder.
- Backlink jobs are tracked in memory: restarting the backlink server forgets job ids (the stage process keeps running; its log is in `mcp_server/job_logs`).
- Traffic is plain HTTP inside the LAN. Don't port-forward these ports to the internet; use a tunnel with proper auth if you later need outside access.
- Rotate the token by deleting `%USERPROFILE%\.seo_mcp_token`, re-running the setup script, and restarting both tasks.

## Hosting on a Mac instead

The Python code is cross-platform; the Mac uses `setup_mac_host.sh` + `start_*_mcp_lan.sh` instead of the `.ps1`/`.bat` files.

1. On the PC run `powershell -ExecutionPolicy Bypass -File package_for_mac.ps1` -> `Desktop\seo-mcp-mac.zip`.
   Copy it to the Mac and unpack with `ditto -x -k ~/Downloads/seo-mcp-mac.zip ~/` (gives `~/seo-mcp/`).
   The 36 GB pipeline inputs stay on the PC; pipeline stages (`start_stage`) won't run on the Mac without them.
2. Install Python 3.10+ from python.org, then run /Applications/Python 3.x/Install Certificates.command.
3. Terminal: `cd ~/seo-mcp/"SEO System" && bash setup_mac_host.sh` (enter your Mac password when asked).
4. Check: `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/mcp` -> 401. Same for 8766.
5. No fixed IP needed: teammates can use `http://<LocalHostName>.local:8765/mcp` (name shown in System Settings > General > Sharing). Check from their PC with `ping <name>.local`. Laptops: keep plugged in; a closed lid sleeps unless an external display is attached.

Logs: `~/Library/Logs/seo-mcp/`. Restart a service: `sudo launchctl kickstart -k system/com.bigriver.seo-mcp.crawler`
(or `...backlink`). Remove: `sudo launchctl bootout system /Library/LaunchDaemons/com.bigriver.seo-mcp.crawler.plist`.
