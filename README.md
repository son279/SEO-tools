# SEO Crawler & Competitor Intelligence System

Big River Digital, Memphis, TN

A Python SEO pipeline that crawls a site and its competitors, turns pages into clean Markdown and LLM-sized chunks, and builds weighted TF-IDF keyword-gap reports. Claude (or a local LLM) can read the results directly, or drive the whole thing through an MCP server.

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

`seo_mcp_server.py` lets Claude run crawls and read the reports.

| Tool | Purpose |
|---|---|
| `run_seo_crawl` | Crawl a primary site + up to 4 competitors (runs in the background by default) |
| `get_crawl_status` | Poll a background crawl |
| `get_keyword_gap_report`, `get_keyword_gap_csv`, `get_primary_top_keywords` | Keyword results |
| `get_ai_audit_packet` | Compact packet for the audit prompts |
| `list_crawl_outputs` | Files produced by a crawl |

**Local (stdio).** Add this to `claude_desktop_config.json` using absolute paths. The file lives at `%APPDATA%\Claude\` on Windows and `~/Library/Application Support/Claude/` on macOS.

```json
{
  "mcpServers": {
    "seo-crawler": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/seo_mcp_server.py"]
    }
  }
}
```

On Windows, use `.venv\\Scripts\\python.exe` and escape the backslashes. Restart Claude Desktop after saving.

**Shared on the office network (HTTP + bearer token).** Run `python seo_mcp_server.py --http --port 8765 --token <secret>`, or use the host setup scripts:

- [LAN_MCP_SETUP.md](LAN_MCP_SETUP.md): Windows host, teammate configs, token rotation.
- [MAC_PIPELINE_SETUP.md](MAC_PIPELINE_SETUP.md): full macOS install, running as a LaunchDaemon, and performance tuning.

The LAN mode is plain HTTP for trusted local networks only. Don't expose ports 8765/8766 to the internet.

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
- client deliverables (`*_competitor_keyword_opportunities.csv`, `clients.txt`, `Claude outputs/`)
- credentials (`client_secret*.json`, `service_account*.json`, `token.pickle`, `.env`, `.seo_mcp_token`, `*.pem`, `*.key`)
- build artifacts, virtualenvs and backups

Contributor notes:

- Keep results in `seo_output_*` folders so they stay ignored. If you've already committed a file, ignoring it isn't enough: `git rm --cached <file>`.
- Commit with your GitHub noreply email: `git config user.email "<id>+<user>@users.noreply.github.com"`.
- Set a real contact in the crawler's `USER_AGENT` (`seo_crawler.py`) before crawling sites you don't own.

---

## Notes

- Built for **LLM-assisted SEO**: Claude or a local LLM does the analysis and rewriting; the crawler collects and scores.
- Only sitemap URLs on the target domains are crawled; external links are not followed.

## License

MIT License, free for commercial and private use.
