# SEO Tools on macOS: Install, Run as MCP, Tune

One MCP server, `seo-tools` (`seo_mcp_server.py`), exposes two sets of tools:

| Tool set | Code | What it does |
|---|---|---|
| Crawler | `seo_crawler.py`, `cli.py` | Sitemap crawl, Markdown + chunks, TF-IDF keyword gap, AI audit packet |
| Backlinks | `backlinks/` | Common Crawl host graph, backlink index, follow/nofollow check, deep gen-1 check, visual graph, gap analysis |

Backlink **data** lives outside the code in `BACKLINK_DATA_DIR`, which defaults to `<project>/backlink_data`. The folder holds `clients.txt`, the Common Crawl downloads, and everything the pipeline generates.

You can run the server two ways. In **local (stdio)** mode, Claude Desktop on this Mac starts it. In **LAN (HTTP)** mode, the Mac runs it as a background service and teammates connect over the network.

---

## 1. System requirements

| | Crawler only | Crawler + backlinks |
|---|---|---|
| macOS | 12 Monterey or newer (Apple Silicon or Intel) | same |
| Python | 3.10+ (3.12 recommended) | same |
| RAM | 8 GB | 16 GB+ recommended |
| Free disk | ~2 GB | **~100 GB** (Common Crawl host graph ~15 GB + generated index up to ~35 GB + ranks file ~5 GB) |
| Network | Outbound HTTPS | Outbound HTTPS, plus high concurrency for `followup_check` / `gen1_deep_check` |
| Node.js | Only on teammate machines (for `npx mcp-remote`) | same |

The RAM figures are working estimates. The heavy stages stream through SQLite and CSV instead of holding everything in memory, but a slow external drive will slow down `backlink_index` and `graph_filter`.

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

macOS privacy protection blocks background services from reading **Downloads, Documents and Desktop**, so keep the project and the data somewhere else:

```
~/seo-mcp/
└── SEO System/            (this repo)
    └── backlink_data/     (default data folder; or point BACKLINK_DATA_DIR elsewhere)
```

To get the project onto the Mac, use one of these:

- `git clone` the repo into `~/seo-mcp/SEO System`.
- From a Windows PC, run `powershell -ExecutionPolicy Bypass -File package_for_mac.ps1 -IncludeBacklink` to create `Desktop\seo-mcp-mac.zip`. That bundles the code plus the backlink query data, but not the raw Common Crawl inputs. Then unpack it on the Mac with `ditto -x -k ~/Downloads/seo-mcp-mac.zip ~/`.

The raw inputs (`host_vertices/`, `host_edges_only/`, the host-ranks `.txt.gz`) are tens of GB. Copy them into the data folder only if this Mac will re-run the early stages. The `extract` stage can also download them.

### 2.3 Python environment

```bash
cd ~/seo-mcp/"SEO System"
python3 -m venv .venv-mac
.venv-mac/bin/pip install --upgrade pip
.venv-mac/bin/pip install -r requirements.txt
#   crawler: requests, beautifulsoup4, google-* (GA4/Search Console script), mcp[cli]<2
#   backlinks: aiohttp, networkx, tldextract
.venv-mac/bin/python -c "import mcp, bs4, aiohttp, networkx; print('ok')"
```

---

## 3. Run as an MCP server

### Option A: Local, for Claude Desktop on this Mac (stdio)

Open **Settings → Developer → Edit Config** in Claude Desktop. The file is `~/Library/Application Support/Claude/claude_desktop_config.json`. Replace `YOURNAME` with your username:

```json
{
  "mcpServers": {
    "seo-tools": {
      "command": "/Users/YOURNAME/seo-mcp/SEO System/.venv-mac/bin/python",
      "args": ["/Users/YOURNAME/seo-mcp/SEO System/seo_mcp_server.py"],
      "env": { "BACKLINK_DATA_DIR": "/Users/YOURNAME/seo-mcp/SEO System/backlink_data" }
    }
  }
}
```

Quit Claude Desktop fully (⌘Q) and reopen it. You'll get one connector with all the crawler and backlink tools.

For Claude Code:

```bash
claude mcp add seo-tools -e BACKLINK_DATA_DIR="$HOME/seo-mcp/SEO System/backlink_data" \
  -- "$HOME/seo-mcp/SEO System/.venv-mac/bin/python" "$HOME/seo-mcp/SEO System/seo_mcp_server.py"
```

### Option B: LAN host, an always-on service for the team (HTTP)

```bash
cd ~/seo-mcp/"SEO System"
bash setup_mac_host.sh                                         # data in ./backlink_data
BACKLINK_DATA_DIR=/Volumes/SSD/backlink_data bash setup_mac_host.sh   # or elsewhere
```

The script asks for your Mac password. It then:

- creates `.venv-mac` and installs the requirements
- generates a shared token at `~/.seo_mcp_token`
- installs a LaunchDaemon (`com.seotools.mcp`) that starts at boot, restarts on crash, raises the open-file limit to 10240 and sets `BACKLINK_DATA_DIR`
- lets Python through the firewall
- disables sleep
- removes the older two-server daemons if present

It prints the IP, URL and token. After that:

1. Reserve the Mac's IP in the router, or use `http://<LocalHostName>.local:8765/mcp`.
2. Check it: `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/mcp` should print **401**.
3. If it's a laptop, keep it plugged in. A closed lid sleeps unless an external display is attached.

To run it in a terminal instead, for debugging: `bash start_seo_mcp_lan.sh`, or `.venv-mac/bin/python seo_mcp_server.py --http --port 8765 --token "$(cat ~/.seo_mcp_token)"`.

**Teammate config.** This needs Node.js. Replace `HOST` and `TOKEN`:

```json
{
  "mcpServers": {
    "seo-tools": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST:8765/mcp", "--allow-http",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer TOKEN" }
    }
  }
}
```

For Claude Code: `claude mcp add --transport http seo-tools http://HOST:8765/mcp --header "Authorization: Bearer TOKEN"`

**Managing the service:**

| Task | Command |
|---|---|
| Logs | `tail -f ~/Library/Logs/seo-mcp/seo-tools.log` |
| Restart | `sudo launchctl kickstart -k system/com.seotools.mcp` |
| Stop / remove | `sudo launchctl bootout system /Library/LaunchDaemons/com.seotools.mcp.plist` |
| Status | `sudo launchctl print system/com.seotools.mcp \| grep state` |
| Rotate token | `rm ~/.seo_mcp_token && bash setup_mac_host.sh`, then send teammates the new token |

Security: this is plain HTTP and only safe inside the office network. Never port-forward 8765. For outside access, use Tailscale or a tunnel with real authentication.

---

## 4. Using the tools

### 4.1 Crawler

| Tool | Use |
|---|---|
| `run_seo_crawl(primary, competitors≤4, output_dir, threads, delay, max_ngram, min_df, top_terms)` | Start a crawl (background by default) |
| `get_crawl_status(output_dir)` | Poll until done |
| `get_keyword_gap_report`, `get_keyword_gap_csv`, `get_primary_top_keywords` | Keyword results |
| `get_ai_audit_packet` | Packet for the audit prompts |
| `list_crawl_outputs` | Files produced |

Output goes to `seo_output_<name>/`: `primary/` and `competitor_N/` (each with `markdown/` and `chunks/`), `keyword_reports/`, `keyword_gap_report.json`, `ai_audit_packet.txt`, `crawl.log`.

You can also crawl without MCP: `.venv-mac/bin/python cli.py https://site.com https://competitor.com --output seo_output_site`

### 4.2 Backlinks

**Start with `backlink_pipeline_status`.** It shows the data folder, the clients, which stages are ready or done, and the next step.

| Tool | Use |
|---|---|
| `backlink_pipeline_status` | Data folder, clients, input sizes, stage readiness, suggested next step |
| `get_backlink_clients` / `set_backlink_clients(hosts, mode)` | Read or edit `clients.txt` (add / remove / replace) |
| `start_stage(stage)` | Run a pipeline stage in the background, one at a time |
| `build_visual_graph(...)` | Visual-graph stage with custom limits |
| `run_backlink_gap_scan(site, competitors)` | Gap analysis straight from Common Crawl files, no index needed |
| `job_status`, `list_backlink_jobs`, `cancel_job` | Track jobs |
| `build_index` | SQLite index needed by the query tools |
| `get_backlink_counts`, `list_backlinks`, `backlink_gap` | Query the index |
| `get_common_crawl_host_rank`, `top_common_crawl_hosts` | Common Crawl harmonic / PageRank metrics |
| `get_followup_summary`, `get_visual_graph` | Follow/nofollow results, graph for drawing |
| `list_backlink_reports`, `read_backlink_report(name, limit, offset, contains)` | Page through any report CSV |

Stage order:

```
extract → backlink_index → graph_filter → followup_check → visual_graph / build_visual_graph → build_index
optional: followup_export (no HTTP), gen1_deep_check (HTTP), count_backlinks,
          graph_export (GraphML/GEXF, high RAM), weighted_graph, export_host_ranks
```

| Stage | Script (`backlinks/`) | Needs (in the data folder) |
|---|---|---|
| `extract` | `extractor_gz.py` | `cc-main-…-host-vertices.paths.gz`; downloads from data.commoncrawl.org |
| `backlink_index` | `backlink_index.py` | `clients.txt`, `host_vertices/`, `host_edges_only/` |
| `graph_filter` / `graph_export` | `graph_filter.py` | `backlink_index/backlink_edges.csv` |
| `followup_check` / `followup_export` | `follow_checker_followup.py` | filtered edges CSV |
| `gen1_deep_check` | `gen1_deep_check.py` | `backlink_index/backlink_edges.csv` |
| `visual_graph` | `visual_graph_filter.py` | follow-up edges CSV |
| `count_backlinks` | `count_backlinks.py` | `clients.txt`, `backlink_edges.csv` |
| `weighted_graph` | `weighted_authority_graph.py` | `backlink_index/follow_status.csv` |
| `export_host_ranks` | `export_host_ranks.py` | `backlink_edges.csv` |

Prompts built into the server: `pipeline_order`, `backlink_audit(site, competitors)` and `full_seo_and_backlink_audit(site, competitors)`.

Job ids live in memory, so restarting the server forgets them. A running stage keeps going, and its log stays in `<data>/job_logs/`.

---

## 5. Tuning

All of these are constants at the top of each script, or tool arguments. After editing, restart the service (`launchctl kickstart -k …`) or Claude Desktop.

### 5.1 Crawler: `seo_crawler.py` (+ `run_seo_crawl` args)

| Setting | Default | Lower it when… | Raise it when… |
|---|---|---|---|
| `threads` / `MAX_WORKERS` | 8 | 8 GB Mac, site rate-limits (429/503) | big sites, strong network (max 32) |
| `delay` / `REQUEST_DELAY_SECONDS` | 0.25 s per domain | (rarely) | site blocks you; `robots.txt` crawl-delay already overrides it |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | 5 / 20 s | fast sites | slow hosts that time out |
| `CHUNK_SIZE_CHARS` | 4000 | smaller local LLMs (try 2000) | long-context models (8000+) |
| `max_ngram` / `TFIDF_MAX_NGRAM` | 3 | faster, less RAM on huge sites | (max 3) |
| `min_df` / `TFIDF_MIN_DF` | 2 | small sites (<20 pages): use 1 | noisy results: 3+ |
| `top_terms` / `TFIDF_REPORT_LIMIT` | 150 | shorter reports | deeper gap lists |
| `THIN_CONTENT_WORDS` | 250 | | stricter thin-content flag |
| `USER_AGENT` | `SEOAuditCrawler/2.0 (+contact: you@example.com)` | **Set a real contact before crawling** | |
| `OUTPUT_DIR` / `SEO_OUTPUT_DIR` env | `~/Documents/SEO Crawler/seo_output` | CLI default only; MCP crawls use `output_dir` | |

### 5.2 Backlinks: `backlinks/*.py`

| File | Setting | Default | Tweak |
|---|---|---|---|
| `follow_checker_followup.py` | `MAX_CONCURRENT_REQUESTS` | 400 | **Mac: start at 150–200**; also needs the open-file limit (5.3) |
| | `CONNECT_TIMEOUT` / `READ_TIMEOUT` / `TOTAL_REQUEST_TIMEOUT` | 5 / 10 / 15 s | Raise on flaky networks |
| | `MAX_PAIRS` | None (all) | e.g. `5000` for a test run |
| `gen1_deep_check.py` | `MAX_PAGES_PER_SOURCE` | 75 | Lower for speed, raise for thoroughness |
| `backlink_index.py` | `MAX_GENERATIONS` | 4 | 2 for a fast first look |
| | `FRONTIER_CAP` | 5000 | Lower = smaller graph |
| | `MAX_WORKERS` | min(8, cores) | Match performance cores |
| | `MAX_EDGE_FILES` / `MAX_VERTEX_FILES` | None | Small numbers to smoke-test |
| `extractor_gz.py` | `MAX_WORKERS` | 8 | Lower on slow connections |
| | `HOST_RANKS_RELEASE` + paths files | `cc-main-2026-jul-aug-sep` / `jun-jul-aug` | Newer Common Crawl release |
| `graph_filter.py` | `BATCH_SIZE` / `WRITE_BATCH_SIZE` | 5k / 20k | Lower on 8 GB machines |
| `build_visual_graph` args | `candidates_per_node`, `non_follow_per_node`, `max_generation`, `clients`, `max_nodes_per_generation` | 15 / 5 / 4 / all / none | Smaller = lighter graph for Gephi |
| `backlinks/config.py` | `MAX_ROWS` | 500 | Max rows per query response |

### 5.3 macOS-specific limits

- **Open files.** macOS allows 256 per process by default, but `followup_check` needs far more and fails with `Too many open files`. The LaunchDaemon from `setup_mac_host.sh` already sets 10240. For terminal runs, `start_seo_mcp_lan.sh` raises it too; otherwise run `ulimit -n 10240` first.
- **Sleep.** Setup runs `pmset -a sleep 0 disksleep 0`. Revert with `sudo pmset -a sleep 10 disksleep 10`.
- **Privacy (TCC).** `Operation not permitted` in the logs means the project or data sits in a protected folder or on an external drive. Move it, or grant Full Disk Access to the venv's real Python binary.
- **Firewall.** If teammates time out instead of getting 401, re-run the `socketfilterfw` lines from `setup_mac_host.sh`.
- **Data on an external SSD.** Point `BACKLINK_DATA_DIR` at the SSD folder and re-run `setup_mac_host.sh` with it set.

### 5.4 Presets

| Machine | Crawler | Backlinks |
|---|---|---|
| 8 GB MacBook Air | `threads=4`, `delay=0.5` | `MAX_GENERATIONS=2`, `FRONTIER_CAP=1000`, `MAX_CONCURRENT_REQUESTS=100` |
| 16 GB M-series | defaults | `MAX_CONCURRENT_REQUESTS=200` |
| 32 GB+ Mac Studio / mini (host) | `threads=12–16` | defaults, data on internal SSD |

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Connector missing in Claude Desktop | Check JSON syntax and absolute paths, then fully quit with ⌘Q. Logs: `~/Library/Logs/Claude/mcp-server-*.log` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Run `Install Certificates.command` (python.org build) or `pip install --upgrade certifi` |
| `Missing inputs for <stage> in <folder>` | Wrong data folder (check `backlink_pipeline_status` → `data_dir`) or an earlier stage hasn't run |
| `query.sqlite3 not found` / query tools fail | Run `build_index` once |
| Teammate gets 401 | Wrong or old token. Compare with `~/.seo_mcp_token` on the host |
| Teammate times out | Host asleep, firewall, wrong IP, or a different network |
| `Job ... is still running` | One backlink stage at a time. Use `list_backlink_jobs` / `cancel_job` |
| Crawl returns very few pages | Site has no or blocked `sitemap.xml`. Pass the sitemap URL as `primary` |
| `Too many open files` | See 5.3 |
