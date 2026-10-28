"""Backlink tools, registered onto the combined SEO MCP server.

    from backlinks.tools import register
    register(mcp)
"""
from . import admin, config, jobs, queries


def register(mcp):
    # ------------------------------------------------------------------ setup
    @mcp.tool()
    def backlink_pipeline_status() -> dict:
        """Start here for backlinks. Shows the data folder (BACKLINK_DATA_DIR), the
        client list, input sizes, which pipeline stages are ready/done, any running
        job, and the suggested next step."""
        return admin.pipeline_status()

    @mcp.tool()
    def get_backlink_clients() -> list[str]:
        """Client hosts in clients.txt (the sites whose backlinks are analysed)."""
        return admin.read_clients()

    @mcp.tool()
    def set_backlink_clients(hosts: list[str], mode: str = "add") -> dict:
        """Edit clients.txt. mode: add (default), remove, or replace.
        Hosts like www.example.com; URLs are trimmed to the host. Re-run
        stage backlink_index afterwards."""
        return admin.set_clients(hosts, mode)

    # --------------------------------------------------------------- pipeline
    @mcp.tool()
    def start_stage(stage: str) -> str:
        """Start a backlink pipeline stage in the background; returns a job id.
        Order: extract -> backlink_index -> graph_filter -> followup_check ->
        visual_graph (or build_visual_graph) -> then build_index for queries.
        Others: followup_export (rebuild follow-up CSVs, no HTTP), gen1_deep_check
        (deep-crawl direct backlinks' inner pages, HTTP), count_backlinks,
        graph_export (GraphML/GEXF for Gephi), weighted_graph, export_host_ranks.
        followup_check and gen1_deep_check make many outbound HTTP requests.
        One job at a time; poll job_status."""
        if stage == "backlink_gap_scan":
            raise ValueError("Use run_backlink_gap_scan for this stage (it needs the sites).")
        return jobs.start(stage)

    @mcp.tool()
    def build_visual_graph(
        candidates_per_node: int = 15,
        non_follow_per_node: int = 5,
        max_generation: int = 4,
        clients: list[str] | None = None,
        max_nodes_per_generation: int | None = None,
    ) -> str:
        """Start the visual-graph filter as a background job; returns a job id.
        For every node it keeps the strongest `candidates_per_node` incoming backlinks
        per generation (dofollow first, then up to `non_follow_per_node` nofollow/
        sponsored/ugc links in the free slots); only dofollow sources expand to the
        next generation. `clients` limits it to those sites (hostnames, subdomains
        included). Needs the followup_check stage output. Poll job_status, then call
        get_visual_graph."""
        if not 1 <= candidates_per_node <= 100:
            raise ValueError("candidates_per_node must be 1-100.")
        if not 0 <= non_follow_per_node <= candidates_per_node:
            raise ValueError("non_follow_per_node must be 0..candidates_per_node.")
        if not 1 <= max_generation <= 4:
            raise ValueError("max_generation must be 1-4.")
        args = [
            "--per-node", candidates_per_node,
            "--non-follow", non_follow_per_node,
            "--max-generation", max_generation,
        ]
        if clients:
            args += ["--clients", ",".join(queries.normalize_domain(c) for c in clients)]
        if max_nodes_per_generation:
            args += ["--max-nodes-per-gen", max_nodes_per_generation]
        return jobs.start("visual_graph", args)

    @mcp.tool()
    def run_backlink_gap_scan(
        site: str,
        competitors: list[str],
        level: str = "host",
        min_competitors: int = 1,
        release: str = "cc-main-2026-jun-jul-aug",
    ) -> str:
        """Standalone backlink gap straight from the Common Crawl host graph: hosts
        linking to 1-4 competitors but not to `site`. Works without the index
        (streams Common Crawl files when host_vertices/host_edges_only are absent,
        which is slow and downloads several GB). Background job; when it succeeds,
        read results with read_backlink_report('gap_scan'). For sites already in
        clients.txt the indexed backlink_gap tool is much faster."""
        if not 1 <= len(competitors) <= 4:
            raise ValueError("Give 1-4 competitors.")
        if level not in ("host", "domain"):
            raise ValueError("level must be host or domain.")
        args = [queries.normalize_host(site), *(queries.normalize_host(c) for c in competitors),
                "--level", level, "--min-competitors", int(min_competitors),
                "--release", release, "--out", "backlink_index/backlink_gap.csv"]
        (config.INDEX_DIR).mkdir(parents=True, exist_ok=True)
        return jobs.start("backlink_gap_scan", args)

    @mcp.tool()
    def job_status(job_id: str, tail_lines: int = 20) -> dict:
        """State, elapsed time and recent log lines of a backlink job."""
        return jobs.status(job_id, tail_lines)

    @mcp.tool()
    def list_backlink_jobs() -> list[dict]:
        """All backlink jobs started since the server started."""
        return jobs.list_all()

    @mcp.tool()
    def cancel_job(job_id: str) -> dict:
        """Terminate a running backlink job."""
        return jobs.cancel(job_id)

    # ---------------------------------------------------------------- queries
    @mcp.tool()
    def build_index(rebuild: bool = False) -> str:
        """Build the SQLite query index from backlink_edges.csv (one-time, slow).
        Needed by the query tools below."""
        return queries.build_index(rebuild)

    @mcp.tool()
    def get_backlink_counts(domains: list[str]) -> list[dict]:
        """Direct (generation 1) backlink edge count and unique referring hosts per domain,
        including its www/subdomains."""
        return queries.backlink_counts(domains[:100])

    @mcp.tool()
    def list_backlinks(domain: str, generation: int = 1, limit: int = 100) -> list[dict]:
        """List source hosts linking to a domain for a given generation (max 500)."""
        return queries.list_backlinks(domain, generation, limit)

    @mcp.tool()
    def backlink_gap(
        site: str,
        competitors: list[str],
        generation: int = 1,
        min_competitors: int = 1,
        limit: int = 100,
    ) -> dict:
        """Backlink gap from the index: hosts that link to 1-4 competitors but not to
        the site (www and subdomains included), ranked by competitors linked then
        Common Crawl harmonic score; includes each host's harmonic and PageRank
        metrics. Only covers domains present in the extracted graph (max 500 rows).
        For sites outside the graph use run_backlink_gap_scan."""
        return queries.backlink_gap(site, competitors, generation, min_competitors, limit)

    @mcp.tool()
    def get_common_crawl_host_rank(host: str) -> dict | None:
        """Common Crawl harmonic and PageRank metrics for an exact host."""
        return queries.common_crawl_host_rank(host)

    @mcp.tool()
    def top_common_crawl_hosts(metric: str = "cc_pagerank_score", limit: int = 25) -> list[dict]:
        """List hosts by Common Crawl metric; scores sort high-first, ranks low-first."""
        return queries.top_common_crawl_hosts(metric, limit)

    @mcp.tool()
    def get_followup_summary() -> list[dict]:
        """Counts by classification from the follow-up rel check
        (follow, nofollow, sponsored, ugc, not_found_on_homepage, fetch failures)."""
        return queries.followup_summary()

    @mcp.tool()
    def get_visual_graph(
        client: str | None = None,
        max_level: int | None = None,
        link_types: list[str] | None = None,
        limit: int = 500,
    ) -> dict:
        """Nodes and edges of the filtered visual backlink graph, for drawing.
        client: only that client's subtree. max_level: 0 = clients, 1 = their direct
        backlinks, etc. link_types: any of follow, nofollow, sponsored, ugc.
        Returns totals by link type and level, up to `limit` edges (max 500), the
        nodes those edges touch, and the path of the styled GEXF file for Gephi."""
        return queries.visual_graph(client, max_level, link_types, limit)

    # ---------------------------------------------------------------- reports
    @mcp.tool()
    def list_backlink_reports() -> list[dict]:
        """Report CSVs the pipeline can produce, whether each exists, and its size."""
        return admin.list_reports()

    @mcp.tool()
    def read_backlink_report(name: str, limit: int = 100, offset: int = 0,
                             contains: str | None = None) -> dict:
        """Page through a pipeline report CSV (see list_backlink_reports for names,
        e.g. client_counts, gen1_deep_summary, gen1_deep_status, followup_status,
        node_metrics, gap_scan). `contains` keeps rows with that text in any column.
        Max 500 rows per call; use offset to page."""
        return admin.read_report(name, limit, offset, contains)

    @mcp.resource("backlink://generation-summary")
    def generation_summary() -> list[dict]:
        """Edge and host counts per generation."""
        return queries.read_small_csv(config.GENERATION_SUMMARY_CSV)

    # ---------------------------------------------------------------- prompts
    @mcp.prompt()
    def pipeline_order() -> str:
        """How to run the backlink pipeline end to end."""
        return (
            "Call backlink_pipeline_status first. Make sure clients.txt lists the client "
            "hosts (set_backlink_clients). Run stages in order with start_stage, polling "
            "job_status until each succeeds: extract, backlink_index, graph_filter, "
            "followup_check (or followup_export to reuse an existing followup_status.csv). "
            "Then build_visual_graph and get_visual_graph for the drawable graph, and "
            "build_index to enable get_backlink_counts, list_backlinks, backlink_gap and "
            "the Common Crawl rank tools. Optional: gen1_deep_check (inner-page check of "
            "direct backlinks), count_backlinks, graph_export, weighted_graph, export_host_ranks. "
            "Common Crawl rank values are carried from backlink_edges.csv; nothing is recalculated."
        )

    @mcp.prompt()
    def backlink_audit(site: str, competitors: str = "") -> str:
        """Backlink audit of a site against optional comma-separated competitors."""
        comp = [c.strip() for c in competitors.split(",") if c.strip()]
        comp_txt = ", ".join(comp) if comp else "no competitors given"
        return (
            f"Run a backlink audit for {site} (competitors: {comp_txt}).\n"
            "1. backlink_pipeline_status; if the site is not in clients.txt, explain which "
            "stages must run and offer to add it with set_backlink_clients.\n"
            "2. get_backlink_counts for the site and competitors; list_backlinks for the site "
            "(generation 1), noting the strongest referrers by Common Crawl harmonic/PageRank.\n"
            "3. get_followup_summary and read_backlink_report('gen1_deep_summary') if present: "
            "how many direct backlinks are follow vs nofollow/sponsored/ugc or not found.\n"
            "4. If competitors were given: backlink_gap, ranked by number of competitors linked "
            "and authority. Pick the 15 best outreach targets and say why.\n"
            "5. Write the report: profile summary, link quality, gap targets, and a prioritised "
            "outreach plan. Flag any data that is missing or stale instead of guessing."
        )

    @mcp.prompt()
    def full_seo_and_backlink_audit(site: str, competitors: str = "") -> str:
        """Combined on-page (crawler) + off-page (backlink) audit."""
        return (
            f"Do a full SEO audit of {site} with competitors: {competitors or 'none'}.\n"
            "On-page: run_seo_crawl (background), poll get_crawl_status, then use "
            "get_ai_audit_packet and get_keyword_gap_report.\n"
            "Off-page: follow the backlink_audit prompt steps for the same site and competitors.\n"
            "Finish with one prioritised action list that combines content/keyword fixes and "
            "link-building targets, each with expected impact and effort."
        )
