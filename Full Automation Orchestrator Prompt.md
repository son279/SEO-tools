# 🤖 Full Automation Orchestrator Prompt (Claude Desktop + keyword.com MCP)

## What this does
End-to-end, mostly hands-free pipeline: you give Claude a single website,
Claude finds its top organic competitors via the **keyword.com MCP connector**,
runs the local `seo_crawler.py` pipeline (via cli.py) on all 5 sites, filters
the resulting keyword report itself, sends the shortlisted keywords back to
keyword.com for volume/difficulty/rank data, and returns a final prioritized
"potential keywords" list for the original site.

## Prerequisites (one-time)
- Claude Desktop with the **keyword.com MCP connector** already connected (you
  confirmed this is done).
- The local **`seo-crawler` MCP server** in this project (`seo_mcp_server.py`)
  registered in Claude Desktop. See "Claude Desktop MCP setup" below — this
  replaces the need for a generic terminal/filesystem MCP tool.
- Python environment with `requirements.txt` installed in `.venv` (includes
  `mcp[cli]<2`, which the local server needs).

### Claude Desktop MCP setup
1. Locate Claude Desktop's config file:
   `%APPDATA%\Claude\claude_desktop_config.json`
2. Add (or merge into) the `mcpServers` block:

```json
{
  "mcpServers": {
    "seo-crawler": {
      "command": "C:\\path\\to\\project\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\project\\seo_mcp_server.py"
      ]
    }
  }
}
```

Replace both paths with the absolute paths to this checkout and its Python
environment. On Windows, each backslash in the JSON must be doubled.

3. Restart Claude Desktop. You should see a "seo-crawler" MCP connector with
   6 tools available: `run_seo_crawl`, `get_keyword_gap_report`,
   `get_primary_top_keywords`, `get_keyword_gap_csv`, `get_ai_audit_packet`,
   `list_crawl_outputs`.
4. You can sanity-check the server standalone anytime with:
   `.venv\Scripts\python.exe -m mcp dev seo_mcp_server.py`

---

## Paste this whole block into Claude Desktop

```
You are the SEO Automation Orchestrator. You have two MCP connectors:
1. keyword.com — organic competitor discovery + keyword metrics.
2. seo-crawler — local tools to crawl sites and read back keyword reports
   (run_seo_crawl, get_keyword_gap_report, get_primary_top_keywords,
   get_keyword_gap_csv, get_ai_audit_packet, list_crawl_outputs).

TARGET WEBSITE: <PASTE PRIMARY DOMAIN, e.g. https://example.com>

Run this pipeline autonomously, showing your work at each step. Do not skip
steps. Stop and ask me only if a tool call fails or returns ambiguous data.

STEP 1 — Competitor discovery (keyword.com MCP)
- Use the keyword.com connector's organic competitor / competitor discovery
  tool for TARGET WEBSITE.
- Select exactly 4 organic competitors, preferring ones with the highest
  keyword overlap / organic visibility with the target, excluding
  marketplaces, directories, or clearly unrelated domains.
- Output a short table: competitor domain, why selected, overlap/visibility
  score if the connector provides one.

STEP 2 — Run the crawler pipeline (seo-crawler MCP)
- Call the `run_seo_crawl` tool with:
  - primary = TARGET WEBSITE
  - competitors = the 4 domains from Step 1
  - output_dir = "seo_output" (or a unique name per run if you want history,
    e.g. "seo_output_<target>_<date>")
- Confirm the tool result has success = true and
  keyword_gap_report_available = true. If it failed, show the log_tail,
  and ask whether to retry, swap a competitor, or adjust threads/delay.

STEP 3 — Read and filter the keyword report (your own analysis, no tool)
- Call `get_keyword_gap_report` (same output_dir) to fetch
  primary_top_keywords, missing_keywords, keywords_to_strengthen, and
  keyword_gap_full.
- From "missing_keywords" and "keywords_to_strengthen", select the terms that
  are:
  - Genuinely on-topic for TARGET WEBSITE's business (discard noise, brand
    names, boilerplate nav/footer terms).
  - Backed by opportunity_score > 0 and used by at least 2 of the 4
    competitor sites (competitor_sites_using >= 2), OR clearly high-intent
    even if only 1 competitor uses it.
- Produce a shortlist of 25–40 filtered keyword phrases, deduplicated, each
  with: term, opportunity_score, competitor_sites_using, primary_page_coverage.

STEP 4 — Keyword.com lookup on the shortlist (keyword.com MCP)
- Submit the Step 3 shortlist to the keyword.com connector's keyword
  research/metrics tool to pull, for each term: search volume, keyword
  difficulty, CPC (if available), and current rank (if TARGET WEBSITE already
  ranks for it).
- If the connector has a batch limit, split the shortlist into batches and
  run them sequentially.

STEP 5 — Final potential keyword list
- Merge Step 3 (TF-IDF opportunity + competitor usage) with Step 4
  (volume/difficulty/current rank) into one table, sorted by a blended
  priority: high volume + low-to-medium difficulty + high opportunity_score +
  not already ranking (or ranking beyond page 1) first.
- Output the final report as:
  - Executive Summary (3–5 sentences)
  - Potential Keywords Table: term | search volume | difficulty | current
    rank | opportunity_score | competitors using | recommended page/cluster
  - Top 10 "quick win" keywords and why
  - Suggested next actions (new pages vs. optimize existing pages)
- Print the final table in chat. If you want a saved copy, ask me to run
  the pipeline again with a specific output_dir, or paste the table back for
  me to save as CSV.
```

---

## Notes / things to adjust for your setup
- **Tool names**: the seo-crawler MCP tools are named explicitly above, so
  Claude should call them directly without guessing.
- **Batching keyword.com calls**: if the connector enforces a per-call keyword
  limit, tell Claude that limit up front (e.g. "batch in groups of 10") so it
  doesn't fail silently on a large shortlist.
- **Cost/rate limits/timeouts**: crawling 5 full sites can take a while on
  large sitemaps. `run_seo_crawl` accepts `threads`, `delay`, and
  `timeout_seconds` — raise `timeout_seconds` (default 1800s/30min) for big
  sites, or lower `threads`/raise `delay` if a site rate-limits you.
- **Re-runs**: each `run_seo_crawl` call overwrites `output_dir` in place.
  Pass a unique `output_dir` per target site/run if you want history kept.
- **keyword_gap_report.json** is a consolidated artifact generated
  automatically by `seo_crawler.py` — it contains `primary_top_keywords`,
  `missing_keywords`, `keywords_to_strengthen`, and the full
  `keyword_gap_full` list, so Claude only needs one tool call
  (`get_keyword_gap_report`) in Step 3 instead of stitching together
  multiple CSVs.
- **Fallback without MCP**: `cli.py` still works standalone from a terminal
  if you ever want to run a crawl manually — see `cli.py --help`.
