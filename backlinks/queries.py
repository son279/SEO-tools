"""SQLite query layer over the large backlink CSVs."""
import csv
import sqlite3
import sys

from . import config

csv.field_size_limit(sys.maxsize)


def reverse_host(host):
    return ".".join(reversed(host.split(".")))


def normalize_domain(value):
    value = value.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]
    if not value or any(c.isspace() for c in value):
        raise ValueError(f"Invalid domain: {value!r}")
    return value[4:] if value.startswith("www.") else value


def normalize_host(value):
    value = value.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]
    if not value or any(c.isspace() for c in value):
        raise ValueError(f"Invalid host: {value!r}")
    return value


def connect():
    if not config.QUERY_DB.exists():
        raise FileNotFoundError("Query index not built; call build_index first.")
    return sqlite3.connect(f"file:{config.QUERY_DB}?mode=ro", uri=True)


def build_index(rebuild=False):
    if config.QUERY_DB.exists():
        if not rebuild:
            with sqlite3.connect(config.QUERY_DB) as existing:
                tables = {
                    row[0]
                    for row in existing.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            if {"edges", "host_ranks"}.issubset(tables):
                return "Index already exists."
            raise RuntimeError(
                "The query index predates Common Crawl host ranks. "
                "Call build_index(rebuild=True) to recreate it."
            )
        config.QUERY_DB.unlink()
    tmp = config.QUERY_DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(
        """
        CREATE TABLE edges (generation INTEGER, source_id INTEGER, source TEXT,
                            target_id INTEGER, target TEXT,
                            source_cc_harmonic_rank INTEGER,
                            source_cc_harmonic_score REAL,
                            source_cc_pagerank_rank INTEGER,
                            source_cc_pagerank_score REAL,
                            target_cc_harmonic_rank INTEGER,
                            target_cc_harmonic_score REAL,
                            target_cc_pagerank_rank INTEGER,
                            target_cc_pagerank_score REAL);
        CREATE TABLE host_ranks (
            host TEXT PRIMARY KEY,
            cc_harmonic_rank INTEGER,
            cc_harmonic_score REAL,
            cc_pagerank_rank INTEGER,
            cc_pagerank_score REAL
        );
        """
    )
    with open(config.EDGES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        edge_batch = []
        rank_batch = []
        for row in reader:
            edge_batch.append(
                (
                    int(row["generation"]),
                    int(row["source_id"]),
                    row["source_domain"],
                    int(row["target_id"]),
                    row["target_domain"],
                    _optional_int(row.get("source_cc_harmonic_rank")),
                    _optional_float(row.get("source_cc_harmonic_score")),
                    _optional_int(row.get("source_cc_pagerank_rank")),
                    _optional_float(row.get("source_cc_pagerank_score")),
                    _optional_int(row.get("target_cc_harmonic_rank")),
                    _optional_float(row.get("target_cc_harmonic_score")),
                    _optional_int(row.get("target_cc_pagerank_rank")),
                    _optional_float(row.get("target_cc_pagerank_score")),
                )
            )
            rank_batch.extend(_edge_host_ranks(row))
            if len(edge_batch) >= 50_000:
                _insert_batches(db, edge_batch, rank_batch)
                edge_batch.clear()
                rank_batch.clear()
        _insert_batches(db, edge_batch, rank_batch)
    db.execute("CREATE INDEX idx_edges_target ON edges(target, generation)")
    db.execute("CREATE INDEX idx_hosts_pagerank ON host_ranks(cc_pagerank_score)")
    db.execute("CREATE INDEX idx_hosts_harmonic ON host_ranks(cc_harmonic_score)")
    db.commit()
    db.close()
    tmp.replace(config.QUERY_DB)
    return "Index built."


def _optional_int(value):
    value = (value or "").strip()
    return int(value) if value else None


def _optional_float(value):
    value = (value or "").strip()
    return float(value) if value else None


def _edge_host_ranks(row):
    output = []
    for prefix in ("source", "target"):
        host = (row.get(f"{prefix}_domain") or "").strip()
        if not host:
            continue
        output.append(
            (
                host,
                _optional_int(row.get(f"{prefix}_cc_harmonic_rank")),
                _optional_float(row.get(f"{prefix}_cc_harmonic_score")),
                _optional_int(row.get(f"{prefix}_cc_pagerank_rank")),
                _optional_float(row.get(f"{prefix}_cc_pagerank_score")),
            )
        )
    return output


def _insert_batches(db, edges, ranks):
    if edges:
        db.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", edges)
    if ranks:
        db.executemany(
            """
            INSERT INTO host_ranks VALUES (?,?,?,?,?)
            ON CONFLICT(host) DO UPDATE SET
                cc_harmonic_rank = COALESCE(host_ranks.cc_harmonic_rank, excluded.cc_harmonic_rank),
                cc_harmonic_score = COALESCE(host_ranks.cc_harmonic_score, excluded.cc_harmonic_score),
                cc_pagerank_rank = COALESCE(host_ranks.cc_pagerank_rank, excluded.cc_pagerank_rank),
                cc_pagerank_score = COALESCE(host_ranks.cc_pagerank_score, excluded.cc_pagerank_score)
            """,
            ranks,
        )


def backlink_counts(domains):
    db = connect()
    result = []
    for raw in domains:
        domain = normalize_domain(raw)
        rev = reverse_host(domain)
        # The bare host and any subdomain (reversed form sorts together).
        row = db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source_id) FROM edges "
            "WHERE generation = 1 AND (target = ? OR (target >= ? AND target < ?))",
            (rev, rev + ".", rev + "/"),
        ).fetchone()
        result.append(
            {"domain": domain, "backlink_count": row[0], "unique_referring_hosts": row[1]}
        )
    db.close()
    return result


def list_backlinks(domain, generation=1, limit=100):
    limit = max(1, min(limit, config.MAX_ROWS))
    rev = reverse_host(normalize_domain(domain))
    db = connect()
    rows = db.execute(
        "SELECT source, target FROM edges WHERE generation = ? "
        "AND (target = ? OR (target >= ? AND target < ?)) LIMIT ?",
        (generation, rev, rev + ".", rev + "/", limit),
    ).fetchall()
    db.close()
    return [
        {"source": reverse_host(s), "target": reverse_host(t)} for s, t in rows
    ]


def _referrers(db, domain, generation):
    """{reversed source host: rank tuple} linking to the domain or its subdomains."""
    rev = reverse_host(domain)
    rows = db.execute(
        "SELECT source, MAX(source_cc_harmonic_rank), MAX(source_cc_harmonic_score), "
        "MAX(source_cc_pagerank_rank), MAX(source_cc_pagerank_score) FROM edges "
        "WHERE generation = ? AND (target = ? OR (target >= ? AND target < ?)) "
        "GROUP BY source",
        (generation, rev, rev + ".", rev + "/"),
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def backlink_gap(site, competitors, generation=1, min_competitors=1, limit=100):
    """Hosts linking to at least `min_competitors` competitors but not to `site`,
    ranked by competitors linked, then Common Crawl harmonic score."""
    if not 1 <= len(competitors) <= 4:
        raise ValueError("Provide between 1 and 4 competitors.")
    limit = max(1, min(limit, config.MAX_ROWS))
    site = normalize_domain(site)
    comps = list(dict.fromkeys(normalize_domain(c) for c in competitors))
    comps = [c for c in comps if c != site]
    if not comps:
        raise ValueError("Competitors must differ from the site.")
    own = [reverse_host(d) for d in [site] + comps]

    db = connect()
    site_refs = _referrers(db, site, generation)
    linked = {}
    ranks = {}
    competitor_totals = {}
    for c in comps:
        refs = _referrers(db, c, generation)
        competitor_totals[c] = len(refs)
        for src, rank in refs.items():
            linked.setdefault(src, []).append(c)
            ranks[src] = rank
    db.close()

    def is_own(src):
        return any(src == o or src.startswith(o + ".") for o in own)

    gap = [
        (src, cs) for src, cs in linked.items()
        if src not in site_refs and not is_own(src) and len(cs) >= min_competitors
    ]
    gap.sort(key=lambda x: (-len(x[1]), -(ranks[x[0]][1] or -1.0), -(ranks[x[0]][3] or -1.0)))
    return {
        "site": site,
        "site_referring_hosts": len(site_refs),
        "competitor_referring_hosts": competitor_totals,
        "gap_total": len(gap),
        "gap": [
            {
                "host": reverse_host(src),
                "competitors_linked": len(cs),
                "competitors": sorted(cs),
                **dict(zip(
                    ("cc_harmonic_rank", "cc_harmonic_score",
                     "cc_pagerank_rank", "cc_pagerank_score"),
                    ranks[src],
                )),
            }
            for src, cs in gap[:limit]
        ],
    }


def common_crawl_host_rank(domain):
    db = connect()
    row = db.execute(
        "SELECT host, cc_harmonic_rank, cc_harmonic_score, "
        "cc_pagerank_rank, cc_pagerank_score FROM host_ranks WHERE host = ?",
        (".".join(reversed(normalize_host(domain).split("."))),),
    ).fetchone()
    db.close()
    if not row:
        return None
    host, *scores = row
    return {
        "host": ".".join(reversed(host.split("."))),
        **dict(zip(
            ("cc_harmonic_rank", "cc_harmonic_score", "cc_pagerank_rank", "cc_pagerank_score"),
            scores,
        )),
    }


def top_common_crawl_hosts(metric="cc_pagerank_score", limit=25):
    metric_columns = {
        "cc_harmonic_score": ("cc_harmonic_score", "DESC"),
        "cc_harmonic_rank": ("cc_harmonic_rank", "ASC"),
        "cc_pagerank_score": ("cc_pagerank_score", "DESC"),
        "cc_pagerank_rank": ("cc_pagerank_rank", "ASC"),
    }
    if metric not in metric_columns:
        raise ValueError(f"Unknown Common Crawl metric: {metric!r}")
    limit = max(1, min(limit, config.MAX_ROWS))
    column, direction = metric_columns[metric]
    db = connect()
    rows = db.execute(
        f"SELECT host, cc_harmonic_rank, cc_harmonic_score, "
        f"cc_pagerank_rank, cc_pagerank_score FROM host_ranks "
        f"WHERE {column} IS NOT NULL ORDER BY {column} {direction} LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [
        {
            "host": ".".join(reversed(r[0].split("."))),
            **dict(zip(
                ("cc_harmonic_rank", "cc_harmonic_score", "cc_pagerank_rank", "cc_pagerank_score"),
                r[1:],
            )),
        }
        for r in rows
    ]


def read_small_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ============================================================
# Follow-up check + visual graph (small CSVs, read directly)
# ============================================================

VISUAL_LINK_TYPES = ("follow", "nofollow", "sponsored", "ugc")


def followup_summary():
    """Classification counts from the standalone follow-up check."""
    if not config.FOLLOWUP_SUMMARY_CSV.exists():
        raise FileNotFoundError(
            "followup_summary.csv not found; run stage followup_check "
            "(or followup_export) first."
        )
    rows = read_small_csv(config.FOLLOWUP_SUMMARY_CSV)
    for row in rows:
        row["count"] = int(row.get("count") or 0)
    return rows


def _visual_files():
    for path in (config.VISUAL_NODES_CSV, config.VISUAL_EDGES_CSV):
        if not path.exists():
            raise FileNotFoundError(
                f"{path.name} not found; call build_visual_graph (or start_stage "
                "visual_graph) and wait for the job to finish."
            )


def _cc_matches(cc_host, prefix):
    return cc_host == prefix or cc_host.startswith(prefix + ".")


def visual_graph(client=None, max_level=None, link_types=None, limit=config.MAX_ROWS):
    """
    Nodes + edges of the visual graph (hostnames in normal form).
    client: keep only the subtree under this client (subdomains included).
    max_level: drop nodes deeper than this generation (0 = clients only).
    link_types: subset of follow/nofollow/sponsored/ugc.
    limit: max edges returned (nodes follow the edges); totals are always full.
    """
    _visual_files()
    limit = max(1, min(int(limit), config.MAX_ROWS))
    types = set(link_types or VISUAL_LINK_TYPES)
    unknown = types - set(VISUAL_LINK_TYPES)
    if unknown:
        raise ValueError(f"Unknown link types {sorted(unknown)}; use {VISUAL_LINK_TYPES}.")

    nodes = {r["domain"]: r for r in read_small_csv(config.VISUAL_NODES_CSV)}
    edges = read_small_csv(config.VISUAL_EDGES_CSV)
    level = {d: int(r.get("level") or 0) for d, r in nodes.items()}

    if client:
        prefix = reverse_host(normalize_domain(client))
        roots = {d for d, lv in level.items() if lv == 0 and _cc_matches(d, prefix)}
        if not roots:
            raise ValueError(f"{client} is not a client in the visual graph.")
        # Walk from the client outward (edges point source -> target).
        incoming = {}
        for e in edges:
            incoming.setdefault(e["target_domain"], []).append(e)
        keep_edges, frontier, seen = [], list(roots), set(roots)
        while frontier:
            nxt = []
            for target in frontier:
                for e in incoming.get(target, ()):
                    keep_edges.append(e)
                    if e["source_domain"] not in seen:
                        seen.add(e["source_domain"])
                        nxt.append(e["source_domain"])
            frontier = nxt
        edges = keep_edges

    def keep(e):
        if (e.get("link_type") or "follow") not in types:
            return False
        if max_level is not None and level.get(e["source_domain"], 0) > max_level:
            return False
        return True

    edges = [e for e in edges if keep(e)]
    edges.sort(key=lambda e: (level.get(e["source_domain"], 0), e["target_domain"]))
    total_edges = len(edges)
    used = set()
    for e in edges:
        used.add(e["source_domain"])
        used.add(e["target_domain"])

    type_counts, level_counts = {}, {}
    for e in edges:
        t = e.get("link_type") or "follow"
        type_counts[t] = type_counts.get(t, 0) + 1
    for d in used:
        lv = level.get(d, 0)
        level_counts[lv] = level_counts.get(lv, 0) + 1

    shown = edges[:limit]
    shown_nodes = set()
    for e in shown:
        shown_nodes.add(e["source_domain"])
        shown_nodes.add(e["target_domain"])

    def node_out(d):
        r = nodes.get(d, {})
        return {
            "host": reverse_host(d),
            "level": level.get(d, 0),
            "role": r.get("role", ""),
            "cc_harmonic_rank": _optional_int(r.get("cc_harmonic_rank")),
            "cc_pagerank_rank": _optional_int(r.get("cc_pagerank_rank")),
        }

    return {
        "total_nodes": len(used),
        "total_edges": total_edges,
        "edges_by_link_type": type_counts,
        "nodes_by_level": dict(sorted(level_counts.items())),
        "truncated": total_edges > limit,
        "gexf_file": str(config.VISUAL_GEXF) if config.VISUAL_GEXF.exists() else None,
        "nodes": [node_out(d) for d in sorted(shown_nodes, key=lambda d: (level.get(d, 0), d))],
        "edges": [
            {
                "source": reverse_host(e["source_domain"]),
                "target": reverse_host(e["target_domain"]),
                "generation": int(e.get("generation") or 0),
                "link_type": e.get("link_type") or "follow",
                "weight": _optional_int(e.get("weight")),
            }
            for e in shown
        ],
    }
