"""Build graph metrics and edges in disk-backed batches."""

import csv
import os
import sqlite3
import sys
from pathlib import Path

INPUT_CSV = Path("backlink_index") / "backlink_edges.csv"
OUTPUT_DIR = Path("backlink_index")
NODE_METRICS_CSV = OUTPUT_DIR / "backlink_node_metrics.csv"


FILTERED_EDGES_FILENAME = "backlink_graph_filtered.csv"
FILTERED_EDGES_OUTPUT = OUTPUT_DIR / FILTERED_EDGES_FILENAME



LEGACY_FILTERED_EDGES_NAMES = (
    "back_link_graph_filtered.csv",
    "backlink_edges_filtered.csv",
)


def _resolve_filtered_edges_csv():
    """Choose the first available filtered-edge CSV from the configured candidates."""
    if FILTERED_EDGES_OUTPUT.exists():
        return FILTERED_EDGES_OUTPUT
    for folder in (OUTPUT_DIR, Path(".")):
        for name in (FILTERED_EDGES_FILENAME, *LEGACY_FILTERED_EDGES_NAMES):
            candidate = folder / name
            if candidate.exists():
                return candidate
    return FILTERED_EDGES_OUTPUT


FILTERED_EDGES_CSV = _resolve_filtered_edges_csv()
FILTERED_GRAPHML = OUTPUT_DIR / "backlink_graph_filtered.graphml"
FILTERED_GEXF = OUTPUT_DIR / "backlink_graph_filtered.gexf"
WORK_DB = OUTPUT_DIR / "graph_filter_work.sqlite3"

CC_RANK_FIELDS = (
    "cc_harmonic_rank",
    "cc_harmonic_score",
    "cc_pagerank_rank",
    "cc_pagerank_score",
)
BATCH_SIZE = 5_000
WRITE_BATCH_SIZE = 20_000


def _node_rank_values(row, prefix):
    """Extract the Common Crawl rank fields for one edge endpoint from a CSV row."""
    return tuple((row.get(f"{prefix}_{field}") or "").strip() for field in CC_RANK_FIELDS)


def _initialize_database(db):
    """Create the SQLite schema used to aggregate graph nodes and edges."""
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA cache_size=-65536")
    db.executescript(
        """
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS nodes;
        DROP TABLE IF EXISTS node_metrics;
        CREATE TABLE edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            weight INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            PRIMARY KEY(source, target)
        ) WITHOUT ROWID;
        CREATE TABLE nodes (
            domain TEXT PRIMARY KEY,
            cc_harmonic_rank TEXT,
            cc_harmonic_score TEXT,
            cc_pagerank_rank TEXT,
            cc_pagerank_score TEXT,
            protected_target INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID;
        """
    )


def _insert_batches(db, edges, nodes, protected_targets):
    """Insert edge and node batches into SQLite while merging node metadata."""
    if nodes:
        db.executemany(
            """
            INSERT INTO nodes VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(domain) DO UPDATE SET
                cc_harmonic_rank = COALESCE(NULLIF(nodes.cc_harmonic_rank, ''), NULLIF(excluded.cc_harmonic_rank, '')),
                cc_harmonic_score = COALESCE(NULLIF(nodes.cc_harmonic_score, ''), NULLIF(excluded.cc_harmonic_score, '')),
                cc_pagerank_rank = COALESCE(NULLIF(nodes.cc_pagerank_rank, ''), NULLIF(excluded.cc_pagerank_rank, '')),
                cc_pagerank_score = COALESCE(NULLIF(nodes.cc_pagerank_score, ''), NULLIF(excluded.cc_pagerank_score, ''))
            """,
            nodes,
        )
    if edges:
        db.executemany(
            """
            INSERT INTO edges VALUES (?, ?, 1, ?)
            ON CONFLICT(source, target) DO UPDATE SET
                weight = edges.weight + 1,
                generation = MIN(edges.generation, excluded.generation)
            """,
            edges,
        )
    if protected_targets:
        db.executemany(
            "UPDATE nodes SET protected_target = 1 WHERE domain = ?",
            ((domain,) for domain in protected_targets),
        )
    db.commit()


def build_database(csv_path=INPUT_CSV):
    """Load and aggregate unique directed edges without holding the graph in RAM."""
    db = sqlite3.connect(WORK_DB)
    _initialize_database(db)
    edges = []
    nodes = []
    protected_targets = []
    row_count = 0

    try:
        with open(csv_path, newline="", encoding="utf-8", buffering=1024 * 1024) as f:
            for row in csv.DictReader(f):
                source = (row.get("source_domain") or "").strip()
                target = (row.get("target_domain") or "").strip()
                if not source or not target or source == target:
                    continue
                try:
                    generation = int(row.get("generation") or 0)
                except ValueError:
                    continue
                if generation <= 0:
                    continue

                edges.append((source, target, generation))
                nodes.append((source, *_node_rank_values(row, "source")))
                nodes.append((target, *_node_rank_values(row, "target")))
                if generation == 1:
                    protected_targets.append(target)
                row_count += 1

                if len(edges) >= BATCH_SIZE:
                    _insert_batches(db, edges, nodes, protected_targets)
                    edges.clear()
                    nodes.clear()
                    protected_targets.clear()
                    print(f"\rLoaded {row_count:,} input edges", end="", flush=True)

        _insert_batches(db, edges, nodes, protected_targets)
        print(f"\rLoaded {row_count:,} input edges")
        db.execute(
            """
            CREATE TABLE node_metrics AS
            SELECT domain,
                   SUM(in_degree) AS in_degree,
                   SUM(out_degree) AS out_degree,
                   SUM(weighted_in_degree) AS weighted_in_degree,
                   SUM(weighted_out_degree) AS weighted_out_degree
            FROM (
                SELECT source AS domain, 0 AS in_degree, COUNT(*) AS out_degree,
                       0 AS weighted_in_degree, SUM(weight) AS weighted_out_degree
                FROM edges GROUP BY source
                UNION ALL
                SELECT target AS domain, COUNT(*) AS in_degree, 0 AS out_degree,
                       SUM(weight) AS weighted_in_degree, 0 AS weighted_out_degree
                FROM edges GROUP BY target
            ) GROUP BY domain
            """
        )
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def _write_csv_batches(db, sql, path, header, transform=None):
    """Stream a database query to a CSV file in batches, optionally transforming each row."""
    temporary = path.with_name(path.name + ".tmp")
    try:
        cursor = db.execute(sql)
        with open(temporary, "w", newline="", encoding="utf-8", buffering=1024 * 1024) as f:
            writer = csv.writer(f)
            writer.writerow(header)
            while True:
                rows = cursor.fetchmany(WRITE_BATCH_SIZE)
                if not rows:
                    break
                if transform:
                    rows = (transform(row) for row in rows)
                writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_node_metrics(db):
    """Export calculated graph metrics and rank fields for every node."""
    _write_csv_batches(
        db,
        """
        SELECT m.domain, m.in_degree, m.out_degree,
               m.weighted_in_degree, m.weighted_out_degree,
               m.in_degree + m.out_degree,
               n.protected_target,
               n.cc_harmonic_rank, n.cc_harmonic_score,
               n.cc_pagerank_rank, n.cc_pagerank_score
        FROM node_metrics AS m JOIN nodes AS n USING(domain)
        """,
        NODE_METRICS_CSV,
        ["domain", "in_degree", "out_degree", "weighted_in_degree",
         "weighted_out_degree", "total_degree", "protected_target", *CC_RANK_FIELDS],
    )


def write_edges(db):
    """Export filtered graph edges with their weights, generations, and endpoint ranks."""
    fields = [
        "source_domain", "target_domain", "weight", "generation",
        *(f"source_{field}" for field in CC_RANK_FIELDS),
        *(f"target_{field}" for field in CC_RANK_FIELDS),
    ]
    _write_csv_batches(
        db,
        """
        SELECT e.source, e.target, e.weight, e.generation,
               s.cc_harmonic_rank, s.cc_harmonic_score,
               s.cc_pagerank_rank, s.cc_pagerank_score,
               t.cc_harmonic_rank, t.cc_harmonic_score,
               t.cc_pagerank_rank, t.cc_pagerank_score
        FROM edges AS e
        JOIN nodes AS s ON s.domain = e.source
        JOIN nodes AS t ON t.domain = e.target
        """,
        FILTERED_EDGES_OUTPUT,
        fields,
    )


def print_summary(db):
    """Print node and edge totals and other aggregate database statistics."""
    node_count = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_count = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    targets = db.execute("SELECT COUNT(*) FROM nodes WHERE protected_target = 1").fetchone()[0]
    print(f"Nodes: {node_count:,}")
    print(f"Edges: {edge_count:,}")
    print(f"Generation-1 target hosts: {targets:,}")
    for generation, count in db.execute(
        "SELECT generation, COUNT(*) FROM edges GROUP BY generation ORDER BY generation"
    ):
        print(f"Generation {generation}: {count:,} edges")


def export_graph(db):
    """Optional full in-memory export; requires substantial RAM for large data."""
    import networkx as nx

    graph = nx.DiGraph()
    for row in db.execute(
        "SELECT domain, cc_harmonic_rank, cc_harmonic_score, cc_pagerank_rank, "
        "cc_pagerank_score, protected_target FROM nodes"
    ):
        domain, *attributes = row
        graph.add_node(domain, **dict(zip((*CC_RANK_FIELDS, "protected_target"), attributes)))
    for source, target, weight, generation in db.execute(
        "SELECT source, target, weight, generation FROM edges"
    ):
        graph.add_edge(source, target, weight=weight, generation=generation)
    nx.write_graphml(graph, FILTERED_GRAPHML)
    nx.write_gexf(graph, FILTERED_GEXF)
    print(f"Graph exports written to {FILTERED_GRAPHML} and {FILTERED_GEXF}")


def main():
    """Build the graph database, calculate/filter graph data, and write the configured exports."""
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"{INPUT_CSV} not found. Run backlink_index.py first.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db = build_database(INPUT_CSV)
    try:
        write_node_metrics(db)
        write_edges(db)
        print_summary(db)
        print(f"Common Crawl host metrics written to {NODE_METRICS_CSV}")
        print(f"Filtered edges written to {FILTERED_EDGES_OUTPUT}")
        if "--export-graph" in sys.argv:
            export_graph(db)
        else:
            print("Skipped GraphML/GEXF export (pass --export-graph to enable; high RAM use).")
    finally:
        db.close()
        WORK_DB.unlink(missing_ok=True)


if __name__ == "__main__":
    main()