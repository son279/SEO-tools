"""Paths and constants for the backlink tools.

Code lives in this package; data lives in a separate data folder so the
(very large) Common Crawl files never sit inside the repo:

    BACKLINK_DATA_DIR   env var, default: <repo>/backlink_data
      clients.txt                       one client host per line
      cc-main-...-host-vertices.paths.gz / ...-host-edges.paths.gz
      cc-main-...-host-ranks.txt.gz
      host_vertices/  host_edges_only/  (extract stage output)
      backlink_index/                   (all generated CSVs, graphs, query DB)
"""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent          # pipeline scripts live here
REPO_DIR = PACKAGE_DIR.parent
ROOT = Path(os.environ.get("BACKLINK_DATA_DIR") or (REPO_DIR / "backlink_data")).expanduser().resolve()
CLIENTS_FILE = ROOT / "clients.txt"
INDEX_DIR = ROOT / "backlink_index"
EDGES_CSV = INDEX_DIR / "backlink_edges.csv"
HOST_RANKS_CSV = INDEX_DIR / "common_crawl_host_ranks.csv"
GENERATION_SUMMARY_CSV = INDEX_DIR / "generation_summary.csv"
FOLLOWUP_SUMMARY_CSV = INDEX_DIR / "followup_summary.csv"
FOLLOWUP_STATUS_CSV = INDEX_DIR / "followup_status.csv"
FOLLOWUP_CLASSIFIED_CSV = INDEX_DIR / "followup_edges_classified.csv"
VISUAL_EDGES_CSV = INDEX_DIR / "visual_graph_edges.csv"
VISUAL_NODES_CSV = INDEX_DIR / "visual_graph_nodes.csv"
VISUAL_GEXF = INDEX_DIR / "visual_graph.gexf"
QUERY_DB = INDEX_DIR / "query.sqlite3"
JOB_LOG_DIR = ROOT / "job_logs"

MAX_ROWS = 500

# Stage name -> (script or [script, *args], required inputs).
# A required entry that is a tuple means "any one of these must exist".
FILTERED_EDGES_ANY = (
    "backlink_index/backlink_graph_filtered.csv",
    "backlink_index/back_link_graph_filtered.csv",
    "backlink_index/backlink_edges_filtered.csv",
)
FOLLOWUP_EDGES_ANY = (
    "backlink_index/followup_edges_classified.csv",
    "backlink_index/followup_edges_dofollow.csv",
)

STAGES = {
    "extract": ("extractor_gz.py", ["cc-main-2026-jun-jul-aug-host-vertices.paths.gz"]),
    "backlink_index": ("backlink_index.py", ["clients.txt", "host_vertices", "host_edges_only"]),
    "graph_filter": ("graph_filter.py", ["backlink_index/backlink_edges.csv"]),
    # Standalone follow-up rel check (outbound HTTP). Writes followup_status.csv,
    # followup_edges_dofollow.csv and followup_edges_classified.csv.
    "followup_check": ("follow_checker_followup.py", [FILTERED_EDGES_ANY]),
    # Rebuild the follow-up edge CSVs from an existing followup_status.csv (no HTTP).
    "followup_export": (
        ["follow_checker_followup.py", "--export-only"],
        [FILTERED_EDGES_ANY, "backlink_index/followup_status.csv"],
    ),
    # ~15 candidates per node per generation for the visual graph (defaults).
    # Use the build_visual_graph tool to pass custom limits.
    "visual_graph": ("visual_graph_filter.py", [FOLLOWUP_EDGES_ANY]),
    "weighted_graph": ("weighted_authority_graph.py", ["backlink_index/follow_status.csv"]),
    "export_host_ranks": (
        "export_host_ranks.py",
        ["backlink_index/backlink_edges.csv"],
    ),
    # GraphML/GEXF export of the filtered graph for Gephi (needs networkx; high RAM).
    "graph_export": (["graph_filter.py", "--export-graph"], ["backlink_index/backlink_edges.csv"]),
    # Deep crawl of generation-1 sources (inner pages, sitemaps). Outbound HTTP.
    # Writes gen1_deep_status.csv and gen1_deep_summary.csv.
    "gen1_deep_check": ("gen1_deep_check.py", ["backlink_index/backlink_edges.csv"]),
    # Direct backlink counts for every host in clients.txt -> clients_backlink_counts.csv.
    "count_backlinks": ("count_backlinks.py", ["clients.txt", "backlink_index/backlink_edges.csv"]),
    # Standalone gap analysis straight from the Common Crawl host graph (no index
    # needed; streams files from data.commoncrawl.org when not present locally).
    # Started through the run_backlink_gap_scan tool, which supplies the sites.
    "backlink_gap_scan": ("backlink_gap.py", []),
}

# Short descriptions shown by the backlink_pipeline_status tool.
STAGE_HELP = {
    "extract": "Download/split the Common Crawl host graph (vertices + edges).",
    "backlink_index": "Build backlink_edges.csv: up to 4 generations of links into clients.txt hosts.",
    "graph_filter": "Node metrics + filtered edge list (backlink_graph_filtered.csv).",
    "graph_export": "Export the filtered graph to GraphML/GEXF for Gephi (high RAM).",
    "followup_check": "Fetch source homepages and classify links follow/nofollow/sponsored/ugc (HTTP).",
    "followup_export": "Rebuild follow-up edge CSVs from an existing followup_status.csv (no HTTP).",
    "gen1_deep_check": "Deep-crawl generation-1 sources to find links on inner pages (HTTP).",
    "visual_graph": "Pick the strongest backlinks per node for a drawable graph (use build_visual_graph for options).",
    "weighted_graph": "Status-weighted edge list for authority scoring (reads follow_status.csv).",
    "export_host_ranks": "One row per host with the Common Crawl harmonic/PageRank metrics.",
    "count_backlinks": "Direct backlink counts for every client -> clients_backlink_counts.csv.",
    "backlink_gap_scan": "Gap analysis straight from Common Crawl files (use run_backlink_gap_scan).",
}

# Report CSVs the read_backlink_report tool may return (name -> file under backlink_index/).
REPORTS = {
    "generation_summary": "generation_summary.csv",
    "node_metrics": "backlink_node_metrics.csv",
    "host_ranks": "common_crawl_host_ranks.csv",
    "client_counts": "clients_backlink_counts.csv",
    "followup_summary": "followup_summary.csv",
    "followup_status": "followup_status.csv",
    "gen1_deep_summary": "gen1_deep_summary.csv",
    "gen1_deep_status": "gen1_deep_status.csv",
    "visual_nodes": "visual_graph_nodes.csv",
    "visual_edges": "visual_graph_edges.csv",
    "authority_weighted_edges": "backlink_edges_authority_weighted.csv",
    "gap_scan": "backlink_gap.csv",
}
