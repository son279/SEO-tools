# SEO Tools: Crawler, Keyword Gap & Backlink Intelligence

Big River Digital, Memphis, TN

A Python SEO toolkit with two halves. **On-page:** it crawls a site and its competitors, turns pages into clean Markdown and LLM-sized chunks, and builds weighted TF-IDF keyword-gap reports. **Off-page:** it maps backlinks from the Common Crawl host graph, checks follow/nofollow, finds backlink gaps against competitors, and builds a drawable link graph. Claude (or a local LLM) can read the results directly, or drive everything through a single MCP server.

---

## What it does

- **Sitemap-driven crawling.** Reads `sitemap.xml`, stays on-site, skips duplicates, and respects `robots.txt` and crawl-delay.
- **Multithreaded.** `ThreadPoolExecutor` with per-domain rate limiting.
- **Markdown extraction.** Titles, headings, paragraphs and lists, converted to clean Markdown.
- **Auto-chunking.** Splits pages into blocks of up to 4,000 characters for Claude, LM Studio, Ollama or vLLM.
- **SEO scoring.** Word count, headings, thin content, images, and keyword frequency per page and per chunk.
- **Competitor keyword extraction.** Up to 4 competitors, crawled the same way as the primary site.
- **Weighted TF-IDF keyword gap.** Terms competitors use that you don't, terms they dominate, and terms you should strengthen.
- **AI audit packet.** A compact text summary built for the Claude prompt pack.
- **Backlink index.** Up to 4 generations of links into your client sites from the Common Crawl host graph, with Common Crawl harmonic centrality and PageRank for every host.
- **Link verification.** Async follow/nofollow/sponsored/ugc checks of source homepages, plus a deep check of direct backlinks' inner pages.
- **Backlink gap.** Hosts linking to competitors but not to you, ranked by overlap and authority, either from the index or straight from Common Crawl.
- **Visual link graph.** The strongest backlinks per node, exportable to GEXF/GraphML for Gephi.

---

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/<OWNER>/<REPO>.git
cd <REPO>
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### Command line

```bash
python cli.py https://example.com https://competitor.example --output seo_output_example
```

### Output layout

```
seo_output_<name>/
├── primary/
│   ├── markdown/            one .md per page
│   └── chunks/<page>/       chunk_1.md, chunk_2.md ... (≤ 4,000 chars)
├── competitor_1/ … competitor_4/
├── keyword_reports/         tfidf_keyword_gap.csv and related reports
├── keyword_gap_report.json
├── ai_audit_packet.txt
├── crawl.log
└── crawl_status.json
```

Output folders are **git-ignored** (see [Data & privacy](#data--privacy)). `sample.json` shows the report format with dummy data.

---

## MCP server (Claude Desktop / Claude Code)

`seo_mcp_server.py` runs one MCP server, **`seo-tools`**, with both tool sets.

**Crawler tools**

| Tool | Purpose |
|---|---|
| `run_seo_crawl` | Crawl a primary site + up to 4 competitors (runs in the background by default) |
| `get_crawl_status` | Poll a background crawl |
| `get_keyword_gap_report`, `get_keyword_gap_csv`, `get_primary_top_keywords` | Keyword results |
| `get_ai_audit_packet` | Compact packet for the audit prompts |
| `list_crawl_outputs` | Files produced by a crawl |

**Backlink tools** (code in `backlinks/`, data in `BACKLINK_DATA_DIR`)

| Tool | Purpose |
|---|---|
| `backlink_pipeline_status` | **Start here.** Data folder, clients, stage readiness, suggested next step |
| `get_backlink_clients`, `set_backlink_clients` | Read or edit `clients.txt` |
| `start_stage` | Run a pipeline stage: `extract`, `backlink_index`, `graph_filter`, `followup_check`, `followup_export`, `gen1_deep_check`, `visual_graph`, `count_backlinks`, `graph_export`, `weighted_graph`, `export_host_ranks` |
| `build_visual_graph` | Visual-graph stage with custom limits |
| `run_backlink_gap_scan` | Gap analysis straight from Common Crawl files (no index needed) |
| `job_status`, `list_backlink_jobs`, `cancel_job` | Track background jobs |
| `build_index` | SQLite index for the query tools |
| `get_backlink_counts`, `list_backlinks`, `backlink_gap` | Query backlinks and gaps |
| `get_common_crawl_host_rank`, `top_common_crawl_hosts` | Common Crawl harmonic / PageRank metrics |
| `get_followup_summary`, `get_visual_graph` | Link-type summary and graph data for drawing |
| `list_backlink_reports`, `read_backlink_report` | Page through any report CSV |

Built-in prompts: `pipeline_order`, `backlink_audit` and `full_seo_and_backlink_audit`.

**Local (stdio).** Add this to `claude_desktop_config.json` using absolute paths. The file lives at `%APPDATA%\Claude\` on Windows and `~/Library/Application Support/Claude/` on macOS.

```json
{
  "mcpServers": {
    "seo-tools": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/seo_mcp_server.py"],
      "env": { "BACKLINK_DATA_DIR": "/absolute/path/to/backlink_data" }
    }
  }
}
```

On Windows, use `.venv\\Scripts\\python.exe` and escape the backslashes. Restart Claude Desktop after saving.

**Shared on the office network (HTTP + bearer token).** Run `python seo_mcp_server.py --http --port 8765 --token <secret>`, or use the host setup scripts:

- [LAN_MCP_SETUP.md](LAN_MCP_SETUP.md): Windows host, teammate configs, token rotation.
- [MAC_PIPELINE_SETUP.md](MAC_PIPELINE_SETUP.md): full macOS install, running as a LaunchDaemon, the backlink pipeline, and performance tuning.

The LAN mode is plain HTTP for trusted local networks only. Don't expose port 8765 to the internet.

---

## Backlink pipeline

The backlink data lives outside the code, in the folder named by `BACKLINK_DATA_DIR` (default `./backlink_data`, which is git-ignored):

```
backlink_data/
├── clients.txt                      one client host per line (edit with set_backlink_clients)
├── cc-main-…-host-vertices.paths.gz / …-host-edges.paths.gz / …-host-ranks.txt.gz
├── host_vertices/  host_edges_only/ Common Crawl host graph (extract stage, ~15 GB)
├── backlink_index/                  generated CSVs, graphs, query.sqlite3
└── job_logs/
```

Run the stages in order, polling `job_status`:

```
extract → backlink_index → graph_filter → followup_check → visual_graph / build_visual_graph → build_index
optional: followup_export, gen1_deep_check, count_backlinks, graph_export, weighted_graph, export_host_ranks
```

`followup_check` and `gen1_deep_check` send many outbound HTTP requests to the source sites. Each script in `backlinks/` can also run on its own from the data folder, for example `cd backlink_data && python ../backlinks/count_backlinks.py`. Tests: `python -m unittest discover -s backlinks/tests -t .`

Backlink data comes from the [Common Crawl web graph](https://commoncrawl.org/web-graphs). Check Common Crawl's terms of use before redistributing derived data.

---

## Claude prompt pack

| Prompt | Use |
|---|---|
| `Primary SEO Audit Prompt.md` | Full audit of the primary site |
| `Competitor Comparison Audit Prompt.md` | Side-by-side with competitors |
| `Keyword Gap Analysis & Improvement Prompt.md` | Act on `keyword_gap_report.json` |
| `Content Strategy Generator Prompt.md` | Content plan from the gaps |
| `Claude Chunk Optimization Prompt.md` | Rewrite individual chunks |
| `Orchestrator Prompt.md`, `Full Automation Orchestrator Prompt.md` | Run the steps end to end |

**Workflow:** crawl → `ai_audit_packet.txt` / `keyword_gap_report.json` → audit prompts → chunk optimization → deploy changes.

---

## Optional: GA4 + Search Console pull

`ga4_cs_data_pull.py` exports Search Console and GA4 page data. It needs your own Google Cloud OAuth client (`client_secret.json`) or a service-account key. These files and the cached `token.pickle` are git-ignored. **Never commit them.**

---

## Data & privacy

Crawl outputs contain scraped page text, including names and email addresses that appear on crawled sites. Client deliverables and credentials must also stay out of the repository. `.gitignore` excludes:

- all crawl outputs (`seo_output*`, `seo_md_output/`, `*.log`, `crawl_status.json`)
- backlink data (`backlink_data/`, `host_vertices/`, `host_edges_only/`, `backlink_index/`, `cc-main-*`, `*.sqlite3`, `*.gexf`, `*.graphml`)
- client deliverables (`*_competitor_keyword_opportunities.csv`, `clients.txt`, `Claude outputs/`)
- credentials (`client_secret*.json`, `service_account*.json`, `token.pickle`, `.env`, `.seo_mcp_token`, `*.pem`, `*.key`)
- build artifacts, virtualenvs and backups

Contributor notes:

- Keep results in `seo_output_*` folders and backlink data in `backlink_data/` (or outside the repo) so they stay ignored. If you've already committed a file, ignoring it isn't enough: `git rm --cached <file>`.
- Commit with your GitHub noreply email: `git config user.email "<id>+<user>@users.noreply.github.com"`.
- Set a real contact in the crawler's `USER_AGENT` (`seo_crawler.py`) before crawling sites you don't own.

---

## Notes

- Built for **LLM-assisted SEO**: Claude or a local LLM does the analysis and rewriting; the crawler collects and scores.
- Only sitemap URLs on the target domains are crawled; external links are not followed.

## License

MIT License, free for commercial and private use.
