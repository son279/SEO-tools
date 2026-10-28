# SEO System on macOS: Install, Run as MCP, Tune

Big River Digital. This covers both halves of the project:

| Part | Folder | MCP name | Port (LAN) | What it does |
|---|---|---|---|---|
| Crawler | `SEO System` | `seo-crawler` | 8765 | Sitemap crawl, Markdown + chunks, TF-IDF keyword gap, AI audit packet |
| Backlinks | `PythonProject5` | `backlink-graph` | 8766 | Common Crawl host graph, backlink index, follow/nofollow check, visual graph |

Each server runs one of two ways. **Local (stdio):** Claude Desktop on the same Mac starts it. **LAN (HTTP):** the Mac runs it as a background service and teammates connect over the network.

---

## 1. System requirements

| | Crawler only | Crawler + backlinks |
|---|---|---|
| macOS | 12 Monterey or newer (Apple Silicon or Intel) | same |
| Python | 3.10+ (3.12 recommended) | same |
| RAM | 8 GB | 16 GB+ recommended |
| Free disk | ~2 GB | **~100 GB** (current data: `backlink_index` 32 GB, `host_edges_only` 14 GB, host-ranks file 4.8 GB, `host_vertices` 1.8 GB) |
| Network | Outbound HTTPS | Outbound HTTPS, plus high concurrency for `followup_check` |
| Node.js | Only on teammate machines (for `npx mcp-remote`) | same |

The RAM figures are working estimates. The heavy stages stream through SQLite and CSV instead of holding everything in memory. A slow external drive will still slow `backlink_index` and `graph_filter` down a lot, so use internal SSD or a fast USB-C/Thunderbolt SSD.

---

## 2. Install

### 2.1 Base tools (one time)

```bash
xcode-select --install                      # compilers for some pip wheels
# Python: either the python.org installer ...
#   then run: /Applications/Python\ 3.12/Install\ Certificates.command
# ... or Homebrew:
brew install python@3.12
brew install node                           # only needed to CONNECT to a LAN server
```

### 2.2 Put the project in the right place

macOS privacy protection blocks background services from reading **Downloads, Documents, and Desktop**, so keep the project somewhere else:

```
~/seo-mcp/
├── SEO System/
└── PythonProject5/
```

You can get the files there two ways:

- **From the Windows PC:** run `powershell -ExecutionPolicy Bypass -File package_for_mac.ps1`, which creates `Desktop\seo-mcp-mac.zip`. Copy the zip to the Mac and unpack it with `ditto -x -k ~/Downloads/seo-mcp-mac.zip ~/`.
- **Directly:** `git clone` or copy both folders. Leave out the Windows `.venv/`, `.idea/`, `__pycache__/`, and old `seo_output*/` folders.

The backlink pipeline inputs (`host_edges_only/`, `host_vertices/`, the host-ranks `.txt.gz`, `backlink_index/`) are about 50 GB and are not in the zip. Copy them separately if this Mac will run backlink stages. Without them, the backlink server still starts, but `start_stage` will report missing inputs.

### 2.3 Python environments

Each folder gets its own `.venv-mac`. This keeps the Windows `.venv` untouched.

```bash
# Crawler
cd ~/seo-mcp/"SEO System"
python3 -m venv .venv-mac
.venv-mac/bin/pip install --upgrade pip
.venv-mac/bin/pip install -r requirements.txt
#   requests, beautifulsoup4, google-api-python-client, google-auth,
#   google-auth-oauthlib, google-analytics-data, mcp[cli]<2

# Backlinks
cd ~/seo-mcp/PythonProject5
python3 -m venv .venv-mac
.venv-mac/bin/pip install --upgrade pip
.venv-mac/bin/pip install "mcp[cli]<2" requests aiohttp networkx tldextract
```

Quick check:

```bash
~/seo-mcp/"SEO System"/.venv-mac/bin/python -c "import mcp, bs4, requests; print('crawler ok')"
~/seo-mcp/PythonProject5/.venv-mac/bin/python -c "import mcp, aiohttp, networkx, tldextract; print('backlink ok')"
```

---

## 3. Run as an MCP server

### Option A: Local, for Claude Desktop on this Mac (stdio)

Open Claude Desktop, go to **Settings → Developer → Edit Config**. The file is `~/Library/Application Support/Claude/claude_desktop_config.json`. Replace `YOURNAME` with your macOS username:

```json
{
  "mcpServers": {
    "seo-crawler": {
      "command": "/Users/YOURNAME/seo-mcp/SEO System/.venv-mac/bin/python",
      "args": ["/Users/YOURNAME/seo-mcp/SEO System/seo_mcp_server.py"]
    },
    "backlink-graph": {
      "command": "/Users/YOURNAME/seo-mcp/PythonProject5/.venv-mac/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/Users/YOURNAME/seo-mcp/PythonProject5"
    }
  }
}
```

Quit Claude Desktop fully (⌘Q), then reopen it. Both connectors should appear under the tools icon.

If `backlink-graph` fails with `No module named mcp_server`, your Claude version is ignoring `cwd`. Use this instead:
`"command": "/bin/bash", "args": ["-c", "cd ~/seo-mcp/PythonProject5 && exec .venv-mac/bin/python -m mcp_server.server"]`

For Claude Code:

```bash
claude mcp add seo-crawler -- ~/seo-mcp/"SEO System"/.venv-mac/bin/python ~/seo-mcp/"SEO System"/seo_mcp_server.py
claude mcp add backlink-graph -- /bin/bash -c "cd ~/seo-mcp/PythonProject5 && exec .venv-mac/bin/python -m mcp_server.server"
```

### Option B: LAN host, an always-on service for the team (HTTP)

One script does it all. It creates the venvs if they're missing, generates a shared token at `~/.seo_mcp_token`, installs two LaunchDaemons (start at boot, restart on crash), lets Python through the firewall, and disables sleep.

```bash
cd ~/seo-mcp/"SEO System"
bash setup_mac_host.sh          # asks for your Mac password
```

It prints the IP, both URLs, and the token. Then:

1. Reserve the Mac's IP in the router (DHCP reservation). You can also use the Bonjour name instead: `http://<LocalHostName>.local:8765/mcp`. The name is under System Settings → General → Sharing.
2. Check it's reachable: `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/mcp` should print **401** (reachable, token required). Do the same for 8766.
3. If the Mac is a laptop, keep it plugged in. Closing the lid puts it to sleep unless an external display is attached.

To run it manually in a terminal (for debugging):

```bash
cd ~/seo-mcp/"SEO System" && bash start_seo_mcp_lan.sh          # :8765
cd ~/seo-mcp/PythonProject5 && bash start_backlink_mcp_lan.sh   # :8766
# or with explicit args:
.venv-mac/bin/python seo_mcp_server.py --http --port 8765 --token "$(cat ~/.seo_mcp_token)"
```

**Teammate config** (Claude Desktop, Mac or PC). This needs Node.js. Replace `HOST` and `TOKEN`:

```json
{
  "mcpServers": {
    "seo-crawler": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST:8765/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    },
    "backlink-graph": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST:8766/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    }
  }
}
```

Claude Code: `claude mcp add --transport http seo-crawler http://HOST:8765/mcp --header "Authorization: Bearer TOKEN"` (same for 8766).

**Managing the services:**

| Task | Command |
|---|---|
| Logs | `tail -f ~/Library/Logs/seo-mcp/crawler.log` (or `backlink.log`) |
| Restart | `sudo launchctl kickstart -k system/com.bigriver.seo-mcp.crawler` (or `.backlink`) |
| Stop / remove | `sudo launchctl bootout system /Library/LaunchDaemons/com.bigriver.seo-mcp.crawler.plist` |
| Status | `sudo launchctl print system/com.bigriver.seo-mcp.crawler \| grep state` |
| Rotate token | `rm ~/.seo_mcp_token && bash setup_mac_host.sh`, then send teammates the new token |

Security: this is plain HTTP and only safe inside the office network. Never port-forward 8765 or 8766. For access from outside, use Tailscale or a tunnel with real authentication.

---

## 4. Running the pipeline

### 4.1 Crawler (`seo-crawler`)

| Tool | Use |
|---|---|
| `run_seo_crawl(primary, competitors≤4, output_dir, threads, delay, max_ngram, min_df, top_terms)` | Starts a crawl in the background by default |
| `get_crawl_status(output_dir)` | Poll until done (reads `crawl.log` / `crawl_status.json`) |
| `get_keyword_gap_report`, `get_keyword_gap_csv`, `get_primary_top_keywords` | Keyword results |
| `get_ai_audit_packet` | Packet that the project's audit prompts use |
| `list_crawl_outputs` | Files produced |

Output layout (`output_dir` resolves relative to `SEO System/`):

```
seo_output_<client>/
├── primary/       markdown/  chunks/<page>/chunk_N.md  (≤4,000 chars)  + page JSON/CSV
├── competitor_1..4/
├── keyword_reports/
├── keyword_gap_report.json
├── ai_audit_packet.txt
├── crawl.log  crawl_status.json
```

The same crawl without MCP: `.venv-mac/bin/python cli.py https://client.com https://competitor.com --output seo_output_client`

Workflow: crawl → `get_ai_audit_packet` → **Primary SEO Audit Prompt** → **Competitor Comparison** → **Keyword Gap** → **Content Strategy** → run **Chunk Optimization** on each `chunks/` file.

### 4.2 Backlinks (`backlink-graph`)

Run the stages in order with `start_stage(stage)`, poll `job_status(job_id)`, and run one job at a time:

```
extract → backlink_index → graph_filter → followup_check → build_visual_graph → build_index
                                         (followup_export reuses an existing followup_status.csv, no HTTP)
optional: weighted_graph, export_host_ranks
```

| Stage | Script | Needs |
|---|---|---|
| `extract` | `extractor_gz.py` | `*.host-vertices.paths.gz`, downloads from data.commoncrawl.org |
| `backlink_index` | `backlink_index.py` | `clients.txt`, `host_vertices/`, `host_edges_only/` |
| `graph_filter` | `graph_filter.py` | `backlink_index/backlink_edges.csv` |
| `followup_check` | `follow_checker_followup.py` | filtered edges CSV; **makes large numbers of outbound HTTP requests** |
| `visual_graph` / `build_visual_graph` | `visual_graph_filter.py` | follow-up edges CSV |

After that, run `build_index` once. It creates `backlink_index/query.sqlite3`, and the queries need it: `get_backlink_counts`, `list_backlinks`, `backlink_gap`, `get_visual_graph`, `get_common_crawl_host_rank`, `top_common_crawl_hosts`, `get_followup_summary`.

To add a client, add its host to `PythonProject5/clients.txt` (one per line, e.g. `www.example.com`) and re-run from `backlink_index`.

Job IDs live in memory, so restarting the backlink server forgets them. The stage process keeps running, and its log stays in `mcp_server/job_logs/`.

---

## 5. Tuning ("tweaking the system requirements")

All of these are constants at the top of each script, or arguments to the tools. Edit, save, then restart the service (`launchctl kickstart -k ...`) or Claude Desktop.

### 5.1 Crawler: `SEO System/seo_crawler.py` (+ `run_seo_crawl` args)

| Setting | Default | Lower it when… | Raise it when… |
|---|---|---|---|
| `threads` (tool arg) / `MAX_WORKERS` | 8 | 8 GB Mac, site rate-limits (429/503) | big sites, strong network (max 32) |
| `delay` (tool arg) / `REQUEST_DELAY_SECONDS` | 0.25 s per domain | (rarely) | site blocks you; `robots.txt` crawl-delay already overrides it |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | 5 / 20 s | fast sites, want quicker failures | slow hosts that time out |
| `CHUNK_SIZE_CHARS` | 4000 | smaller local LLMs (try 2000) | long-context models (8000+) |
| `max_ngram` / `TFIDF_MAX_NGRAM` | 3 | faster, less RAM on huge sites | (max 3) |
| `min_df` / `TFIDF_MIN_DF` | 2 | small sites (<20 pages): use 1 | noisy results: 3+ |
| `top_terms` / `TFIDF_REPORT_LIMIT` | 150 | shorter reports | deeper gap lists |
| `THIN_CONTENT_WORDS` | 250 | | stricter thin-content flag |
| `timeout_seconds` (tool arg) | 1800 | | very large sites in foreground mode |
| `USER_AGENT` | `SEOAuditCrawler/2.0 (+contact: you@example.com)` | **Change this to a real contact email** | |
| `OUTPUT_DIR` / `SEO_OUTPUT_DIR` env | `~/Documents/SEO Crawler/seo_output` | Applies to the CLI default only. Point it outside Documents if a background service runs the CLI | |

### 5.2 Backlinks: `PythonProject5/*.py`

| File | Setting | Default | Tweak |
|---|---|---|---|
| `follow_checker_followup.py` | `MAX_CONCURRENT_REQUESTS` | 400 | **Mac: start at 150–200.** 400 can exceed the open-file limit and swamp home or office routers. See 5.3 |
| | `CONNECT_TIMEOUT` / `READ_TIMEOUT` / `TOTAL_REQUEST_TIMEOUT` | 5 / 10 / 15 s | Raise on flaky networks; lower to finish faster |
| | `MAX_RETRIES_PER_SCHEME`, `TRY_HTTP_FALLBACK` | 1, True | Raise retries if many `connection_failed` |
| | `MAX_PAIRS` | None (all) | Set e.g. `5000` for a test run |
| | `MAX_HTML_BYTES` | 2 MB | Lower to save RAM/bandwidth |
| `backlink_index.py` | `MAX_GENERATIONS` | 4 | 2 for a fast first look |
| | `FRONTIER_CAP` | 5000 | Lower = smaller graph, less disk/RAM |
| | `MAX_WORKERS` | min(8, cores) | Match performance cores (M1/M2 base: 4–6) |
| | `MAX_EDGE_FILES` / `MAX_VERTEX_FILES` | None | Set small numbers to smoke-test on a subset |
| `extractor_gz.py` | `MAX_WORKERS` | 8 | Lower on slow connections |
| | `EXTRACT_DOWNLOADED_FILES` | False | Keep False to save disk |
| | `HOST_RANKS_RELEASE` + paths files | `cc-main-2026-jul-aug-sep` / `jun-jul-aug` | Update to a newer Common Crawl release |
| `graph_filter.py` | `BATCH_SIZE` / `WRITE_BATCH_SIZE` | 5k / 20k | Lower on 8 GB machines |
| `visual_graph_filter.py` / `build_visual_graph` args | `candidates_per_node`, `non_follow_per_node`, `max_generation`, `clients`, `max_nodes_per_generation` | 15 / 5 / 4 / all / none | Smaller = a lighter graph that Gephi can render |
| `mcp_server/config.py` | `MAX_ROWS` | 500 | Max rows returned per query to Claude |

### 5.3 macOS-specific limits

**Open files.** macOS defaults to 256 open files per process, which is far below what `followup_check` needs at high concurrency. The symptom is `Too many open files` / `OSError: [Errno 24]`.

- For a manual or terminal run: `ulimit -n 10240` before starting.
- For the LaunchDaemon: add this to `/Library/LaunchDaemons/com.bigriver.seo-mcp.backlink.plist` inside `<dict>`, then `bootout` + `bootstrap` the daemon:

  ```xml
  <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>10240</integer></dict>
  <key>HardResourceLimits</key><dict><key>NumberOfFiles</key><integer>10240</integer></dict>
  ```

  Alternatively, add `ulimit -n 10240` near the top of `start_backlink_mcp_lan.sh`.

**Other limits:**

- **Sleep:** the setup script runs `pmset -a sleep 0 disksleep 0`. To revert: `sudo pmset -a sleep 10 disksleep 10`.
- **Privacy (TCC):** if logs show `Operation not permitted`, the project is under a protected folder or an external drive. Move it under `~/seo-mcp/`, or grant Full Disk Access to the venv's real Python binary (`.venv-mac/bin/python -c "import os,sys;print(os.path.realpath(sys.executable))"`).
- **Firewall:** if teammates time out (instead of getting 401), re-run the `socketfilterfw --add/--unblockapp` lines from `setup_mac_host.sh` for the venv Python.
- **Ports in use:** `lsof -iTCP:8765 -sTCP:LISTEN`. To change ports, edit `--port` in `start_*_mcp_lan.sh` and teammates' URLs.
- **Data on an external SSD:** keep the code in `~/seo-mcp/`, and symlink the big folders: `ln -s /Volumes/SSD/backlink_index ~/seo-mcp/PythonProject5/backlink_index` (same for `host_edges_only`, `host_vertices`). Give Python Full Disk Access if macOS blocks the volume.

### 5.4 Presets

| Machine | Crawler | Backlinks |
|---|---|---|
| 8 GB MacBook Air | `threads=4`, `delay=0.5` | Crawler only, or `MAX_GENERATIONS=2`, `FRONTIER_CAP=1000`, `MAX_CONCURRENT_REQUESTS=100` |
| 16 GB M-series | defaults | `MAX_CONCURRENT_REQUESTS=200`, `ulimit -n 10240` |
| 32 GB+ Mac Studio / mini (host) | `threads=12–16` | defaults, `MAX_CONCURRENT_REQUESTS=300–400`, `ulimit -n 10240`, data on internal SSD |

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Connector missing in Claude Desktop | Check JSON syntax and absolute paths, then fully quit with ⌘Q. Logs: `~/Library/Logs/Claude/mcp-server-*.log` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Run `Install Certificates.command` (python.org build) or `pip install --upgrade certifi` |
| `No module named mcp_server` | The backlink server must start with `PythonProject5` as its working directory (see 3A) |
| Teammate gets 401 | Wrong or old token. Compare against `~/.seo_mcp_token` on the host |
| Teammate times out | Host asleep, firewall, wrong IP, or the networks aren't the same. Try `ping <name>.local` |
| `Missing inputs for <stage>` | Run the earlier stage first, or copy the data folders over (2.2) |
| `Job ... is still running` | Only one backlink stage runs at a time. Use `job_status` or `cancel_job` |
| Crawl returns very few pages | Site has no or blocked `sitemap.xml`. Pass the sitemap URL directly as `primary` |
| `Too many open files` | See 5.3 |
