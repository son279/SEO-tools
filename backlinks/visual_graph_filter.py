"""
VISUAL BACKLINK GRAPH FILTER -- keeps ~15 best candidates per node per generation.

Pipeline position:
    graph_filter.py  ->  follow_checker_followup.py
                     ->  backlink_index/followup_edges_classified.csv
                     ->  THIS SCRIPT

The verified edge list is far too big to draw. This script walks it outward
from the clients one generation at a time and, for every node in the current
frontier, keeps only its CANDIDATES_PER_NODE strongest incoming backlinks:

    generation 1: client targets                 <- top 15 linking hosts each
    generation 2: those selected gen-1 sources   <- top 15 linking hosts each
    generation 3: those selected gen-2 sources   <- top 15 linking hosts each
    ...

Only hosts picked at one generation become the frontier for the next, so the
graph stays a readable tree-like fan-out instead of millions of nodes.

Link types (from the follow-up rel check):
  * Dofollow links fill each node's slots first.
  * nofollow / sponsored / ugc links may take the slots that are left, up to
    NON_FOLLOW_PER_NODE per node.
  * Only dofollow sources are expanded into the next generation -- authority
    barely flows through a nofollow link, so its own backlinks are not drawn.
  * Every edge carries link_type; the GEXF colours / dashes edges by type.

"Strongest" = best (lowest) Common Crawl harmonic rank of the source host,
then best PageRank rank, then higher edge weight. Hosts without a rank sort last.

Each generation is one streaming pass over the CSV, so RAM only holds the
frontier plus a 15-item heap per frontier node.

Outputs (backlink_index/):
    visual_graph_edges.csv     selected edges (input columns + link_type)
    visual_graph_nodes.csv     one row per node with level, role, ranks, degrees
    visual_graph.gexf          for Gephi            (needs networkx)
    visual_graph.graphml       for Gephi/Cytoscape  (needs networkx)

Usage:
    python visual_graph_filter.py                    # default input
    python visual_graph_filter.py path/to/edges.csv  # any edge CSV with the same columns

Options (override the CONFIG values below; used by the MCP server):
    --per-node N              candidates per node            (CANDIDATES_PER_NODE)
    --non-follow N            non-dofollow slots per node    (NON_FOLLOW_PER_NODE)
    --max-generation N        deepest generation             (MAX_GENERATION)
    --max-nodes-per-gen N     hard node cap per generation   (MAX_NODES_PER_GENERATION)
    --clients a.com,b.com     only these clients             (ONLY_CLIENTS)
    --no-graph-files          skip GEXF/GraphML
"""

import csv
import heapq
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path





OUTPUT_DIR = Path("backlink_index")





INPUT_CANDIDATES = (
    OUTPUT_DIR / "followup_edges_classified.csv",
    OUTPUT_DIR / "followup_edges_dofollow.csv",
)


CANDIDATES_PER_NODE = 15


CANDIDATES_PER_GENERATION = {}



NON_FOLLOW_PER_NODE = 5


NON_FOLLOW_TYPES = {"nofollow", "sponsored", "ugc"}


EDGE_STYLE = {
    "follow":    ((46, 125, 50), "solid"),
    "nofollow":  ((150, 150, 150), "dashed"),
    "sponsored": ((239, 108, 0), "dashed"),
    "ugc":       ((30, 136, 229), "dotted"),
}


MAX_GENERATION = 4



MAX_NODES_PER_GENERATION = None



ONLY_CLIENTS = None



SKIP_ALREADY_PLACED = True

WRITE_GRAPH_FILES = True

EDGES_OUT = OUTPUT_DIR / "visual_graph_edges.csv"
NODES_OUT = OUTPUT_DIR / "visual_graph_nodes.csv"
GEXF_OUT = OUTPUT_DIR / "visual_graph.gexf"
GRAPHML_OUT = OUTPUT_DIR / "visual_graph.graphml"

PROGRESS_EVERY_ROWS = 1_000_000

RANK_MISSING = float("inf")







def to_commoncrawl(host):
    """www.example.com / https://example.com/ -> com.example"""
    host = host.strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return ".".join(reversed([p for p in host.split(".") if p]))


def client_prefixes():
    """Return reversed Common Crawl prefixes for configured client domains, if any."""
    if not ONLY_CLIENTS:
        return None
    return tuple(to_commoncrawl(c) for c in ONLY_CLIENTS)


def is_selected_client(target, prefixes):
    """com.example matches com.example and com.example.www / any subdomain."""
    return any(target == p or target.startswith(p + ".") for p in prefixes)


def parse_args(argv):
    """Apply command-line overrides to the CONFIG globals; return positional args."""
    global CANDIDATES_PER_NODE, NON_FOLLOW_PER_NODE, MAX_GENERATION
    global MAX_NODES_PER_GENERATION, ONLY_CLIENTS, WRITE_GRAPH_FILES
    positional, it = [], iter(argv)
    for arg in it:
        if arg == "--no-graph-files":
            WRITE_GRAPH_FILES = False
            continue
        if not arg.startswith("--"):
            positional.append(arg)
            continue
        if "=" in arg:
            name, value = arg.split("=", 1)
        else:
            name, value = arg, next(it, None)
            if value is None:
                raise SystemExit(f"Missing value for {name}")
        if name == "--per-node":
            CANDIDATES_PER_NODE = max(1, int(value))
        elif name == "--non-follow":
            NON_FOLLOW_PER_NODE = max(0, int(value))
        elif name == "--max-generation":
            MAX_GENERATION = max(1, int(value))
        elif name == "--max-nodes-per-gen":
            MAX_NODES_PER_GENERATION = max(1, int(value)) if int(value) > 0 else None
        elif name == "--clients":
            ONLY_CLIENTS = {c.strip() for c in value.split(",") if c.strip()} or None
        else:
            raise SystemExit(f"Unknown option {name}")
    return positional


def resolve_input_csv(positional=()):
    """Select the graph CSV from positional arguments or the configured input path."""
    if positional:
        return Path(positional[0])
    for candidate in INPUT_CANDIDATES:
        if candidate.exists():
            return candidate
    return INPUT_CANDIDATES[0]


def limit_for(generation):
    """Return the configured candidate cap for a generation."""
    return CANDIDATES_PER_GENERATION.get(generation, CANDIDATES_PER_NODE)


def _num(value, default=RANK_MISSING):
    """Parse a numeric value or return the supplied default for missing or invalid input."""
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def link_type(row):
    """Return a normalized link classification from an edge row."""
    return (row.get("link_type") or "follow").strip().lower() or "follow"


def strength_key(row):
    """Bigger tuple = stronger candidate (heapq is a min-heap, so we keep the max)."""
    return (
        link_type(row) == "follow",
        -_num(row.get("source_cc_harmonic_rank")),
        -_num(row.get("source_cc_pagerank_rank")),
        _num(row.get("weight"), 0.0),
    )


def cc_to_hostname(domain):
    """com.example.www -> www.example.com (for readable labels)."""
    parts = [p for p in (domain or "").split(".") if p]
    if len(parts) <= 1 or parts[-1].isdigit():
        return domain
    return ".".join(reversed(parts))







def select_generation(csv_path, generation, frontier, placed):
    """
    One pass over the CSV. For each frontier target keep the top-N edges of
    this generation pointing at it. Returns (selected_rows, fieldnames).
    """
    n = limit_for(generation)
    clients = client_prefixes() if generation == 1 else None
    n_other = min(NON_FOLLOW_PER_NODE, n)
    heaps = defaultdict(list)        
    other_heaps = defaultdict(list)  
    seq = 0
    scanned = 0
    started = time.perf_counter()
    fieldnames = None

    with open(csv_path, newline="", encoding="utf-8", buffering=1024 * 1024) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            scanned += 1
            if scanned % PROGRESS_EVERY_ROWS == 0:
                rate = scanned / max(time.perf_counter() - started, 0.001)
                print(f"\r  Gen {generation}: scanned {scanned:,} rows "
                      f"({rate:,.0f} rows/s)", end="", flush=True)

            if row.get("generation") != str(generation):
                continue
            target = row.get("target_domain")
            if frontier is not None and target not in frontier:
                continue
            if clients and not is_selected_client(target, clients):
                continue
            source = row.get("source_domain")
            if not source or source == target:
                continue
            if SKIP_ALREADY_PLACED and source in placed:
                continue

            kind = link_type(row)
            if kind == "follow":
                heap, cap = heaps[target], n
            elif kind in NON_FOLLOW_TYPES and n_other > 0:
                heap, cap = other_heaps[target], n_other
            else:
                continue
            row["link_type"] = kind

            item = (strength_key(row), seq, row)
            seq += 1
            if len(heap) < cap:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)

    print(f"\r  Gen {generation}: scanned {scanned:,} rows"
          f"{' ' * 30}")

    selected = []
    for target in sorted(heaps.keys() | other_heaps.keys()):
        follows = [r for _, _, r in sorted(heaps.get(target, ()), reverse=True)]
        free = min(n - len(follows), n_other)
        others = [r for _, _, r in sorted(other_heaps.get(target, ()), reverse=True)]
        selected.extend(follows)
        selected.extend(others[:max(free, 0)])

    if fieldnames is not None and "link_type" not in fieldnames:
        fieldnames = [*fieldnames, "link_type"]

    if MAX_NODES_PER_GENERATION is not None:
        selected = cap_generation(selected, MAX_NODES_PER_GENERATION)

    return selected, fieldnames


def cap_generation(rows, max_nodes):
    """Keep strongest edges until max_nodes distinct sources are in."""
    kept, sources = [], set()
    for row in sorted(rows, key=strength_key, reverse=True):
        src = row["source_domain"]
        if src not in sources and len(sources) >= max_nodes:
            continue
        sources.add(src)
        kept.append(row)
    return kept






NODE_FIELDS = [
    "domain", "hostname", "level", "role", "in_degree", "out_degree",
    "cc_harmonic_rank", "cc_harmonic_score", "cc_pagerank_rank", "cc_pagerank_score",
]
RANK_FIELDS = ("cc_harmonic_rank", "cc_harmonic_score",
               "cc_pagerank_rank", "cc_pagerank_score")


def build_nodes(edges, level_of):
    """Aggregate node attributes and graph connectivity from the selected edges."""
    nodes = {}

    def touch(domain, prefix, row):
        """Create or update one node record using an edge endpoint and its rank metadata."""
        node = nodes.get(domain)
        if node is None:
            level = level_of.get(domain, 0)
            node = nodes[domain] = {
                "domain": domain,
                "hostname": cc_to_hostname(domain),
                "level": level,
                "role": "client" if level == 0 else f"gen{level}_source",
                "link_types": set(),
                "in_degree": 0,
                "out_degree": 0,
                **{f: "" for f in RANK_FIELDS},
            }
        for f in RANK_FIELDS:
            if not node[f]:
                node[f] = row.get(f"{prefix}_{f}") or ""
        return node

    for row in edges:
        src = touch(row["source_domain"], "source", row)
        src["out_degree"] += 1
        src["link_types"].add(row["link_type"])
        touch(row["target_domain"], "target", row)["in_degree"] += 1

    for node in nodes.values():
        types = node.pop("link_types")
        if node["level"] and "follow" not in types:
            node["role"] = f"gen{node['level']}_nonfollow_source"
    return nodes


def write_csv(path, fieldnames, rows):
    """Write CSV rows atomically through a temporary file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def write_graph_files(edges, nodes):
    """Export the selected graph and node data to configured graph files."""
    try:
        import networkx as nx
    except ImportError:
        print("networkx not installed -- skipped GEXF/GraphML (CSV outputs still written).")
        return

    g = nx.DiGraph()
    level_colors = [(198, 40, 40), (21, 101, 192), (0, 131, 143), (106, 27, 154), (93, 64, 55)]
    for d, attrs in nodes.items():
        g.add_node(d, label=attrs["hostname"], level=int(attrs["level"]), role=attrs["role"],
                   cc_harmonic_rank=_num(attrs["cc_harmonic_rank"], -1.0),
                   cc_pagerank_rank=_num(attrs["cc_pagerank_rank"], -1.0),
                   in_degree=attrs["in_degree"], out_degree=attrs["out_degree"])
    for row in edges:
        g.add_edge(row["source_domain"], row["target_domain"],
                   weight=_num(row.get("weight"), 1.0),
                   generation=int(row["generation"]),
                   link_type=row["link_type"])
    nx.write_graphml(g, GRAPHML_OUT)


    for d, data in g.nodes(data=True):
        r, gg, b = level_colors[min(data["level"], len(level_colors) - 1)]
        data["viz"] = {"color": {"r": r, "g": gg, "b": b, "a": 1.0},
                       "size": 20.0 if data["level"] == 0 else max(4.0, 12.0 - 2 * data["level"])}
    for _, _, data in g.edges(data=True):
        (r, gg, b), shape = EDGE_STYLE.get(data["link_type"], EDGE_STYLE["nofollow"])
        data["viz"] = {"color": {"r": r, "g": gg, "b": b, "a": 1.0},
                       "thickness": 2.0 if data["link_type"] == "follow" else 1.0,
                       "shape": shape}
    nx.write_gexf(g, GEXF_OUT, version="1.2draft")
    print(f"Graph files:  {GEXF_OUT}, {GRAPHML_OUT}")







def main():
    """Parse graph-filter options, select and cap edges, then write visual graph exports."""
    input_csv = resolve_input_csv(parse_args(sys.argv[1:]))
    if not input_csv.exists():
        print(f"ERROR: dofollow edge list not found: {input_csv}")
        print("Run follow_checker_followup.py first, or pass the CSV path as an argument.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 78)
    print("VISUAL GRAPH FILTER -- top candidates per node per generation")
    print("=" * 78)
    print(f"Input:            {input_csv}")
    print(f"Per node:         {CANDIDATES_PER_NODE}"
          + (f"  overrides {CANDIDATES_PER_GENERATION}" if CANDIDATES_PER_GENERATION else ""))
    print(f"Non-follow/node:  up to {NON_FOLLOW_PER_NODE} ({', '.join(sorted(NON_FOLLOW_TYPES))})")
    print(f"Max generation:   {MAX_GENERATION}")
    if ONLY_CLIENTS:
        print(f"Only clients:     {', '.join(sorted(ONLY_CLIENTS))}")
    print()

    started = time.perf_counter()
    level_of = {}       
    all_edges = []
    fieldnames = None
    frontier = None     

    for generation in range(1, MAX_GENERATION + 1):
        selected, fieldnames = select_generation(
            input_csv, generation, frontier, level_of.keys()
        )
        if not selected:
            print(f"  Gen {generation}: no edges selected, stopping.")
            break

        if generation == 1:
            for row in selected:
                level_of.setdefault(row["target_domain"], 0)

        new_follow_sources, new_other_sources = set(), set()
        for row in selected:
            src = row["source_domain"]
            if level_of.get(src, generation) == generation:
                level_of[src] = generation
                if row["link_type"] == "follow":
                    new_follow_sources.add(src)
                else:
                    new_other_sources.add(src)
        new_other_sources -= new_follow_sources

        all_edges.extend(selected)
        targets = len({r["target_domain"] for r in selected})
        type_counts = Counter(r["link_type"] for r in selected)
        print(f"  Gen {generation}: {len(selected):,} edges into {targets:,} nodes "
              f"({', '.join(f'{k} {v:,}' for k, v in type_counts.most_common())}); "
              f"{len(new_follow_sources):,} dofollow sources expand, "
              f"{len(new_other_sources):,} non-follow leaves")


        frontier = new_follow_sources
        if not frontier:
            break

    if not all_edges:
        print("\nNothing selected. Check that the input has generation-1 rows.")
        return

    nodes = build_nodes(all_edges, level_of)
    write_csv(EDGES_OUT, fieldnames, all_edges)
    write_csv(NODES_OUT, NODE_FIELDS, sorted(nodes.values(),
                                             key=lambda n: (n["level"], n["domain"])))
    if WRITE_GRAPH_FILES:
        write_graph_files(all_edges, nodes)

    clients = sum(1 for n in nodes.values() if n["level"] == 0)
    print("=" * 78)
    print(f"Clients:      {clients:,}")
    print(f"Nodes:        {len(nodes):,}")
    print(f"Edges:        {len(all_edges):,}")
    for kind, count in Counter(r["link_type"] for r in all_edges).most_common():
        print(f"  {kind:11} {count:,}")
    print(f"Elapsed:      {time.perf_counter() - started:,.1f}s")
    print(f"Edges CSV:    {EDGES_OUT}")
    print(f"Nodes CSV:    {NODES_OUT}")


if __name__ == "__main__":
    main()
