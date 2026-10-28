#!/usr/bin/env python3
"""Backlink gap analysis using the Common Crawl web graph.

Finds domains/hosts that link to at least one competitor (max 4) but not to
your site, ranked by how many competitors they link to and by Common Crawl's
harmonic centrality / PageRank.

Usage:
    python backlink_gap.py mysite.com comp1.com comp2.com [comp3.com comp4.com]
        [--release cc-main-2026-jun-jul-aug] [--level host|domain]
        [--vertices-dir DIR] [--edges-dir DIR] [--ranks-file FILE]
        [--data-dir DIR] [--min-competitors 1] [--top 50] [--out gap.csv]

Split files in host_vertices/ and host_edges_only/ are used when present.
Otherwise files are streamed from data.commoncrawl.org unless a copy with the
expected name exists in --data-dir.
"""
import argparse
import csv
import gzip
import os
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph/{rel}/{lvl}/{rel}-{lvl}-{kind}.txt.gz"


def graph_parts_dir(args, kind):
    """Return the local part directory for the requested graph data kind."""
    if kind == "vertices":
        return args.vertices_dir
    if kind == "edges":
        return args.edges_dir
    return None


@contextmanager
def open_graph_file(args, kind):
    """Open local split/monolithic data when available, otherwise stream it."""
    parts_dir = graph_parts_dir(args, kind)
    if parts_dir is not None:
        parts = sorted(parts_dir.glob("*.gz"))
        if parts:
            log(f"Using {len(parts):,} extracted {kind} part(s) in {parts_dir}")

            def iter_parts():
                """Yield decompressed lines from each sorted gzip part, reporting read failures with the part directory."""
                try:
                    for path in parts:
                        with gzip.open(path, "rb") as f:
                            yield from f
                except OSError as exc:
                    raise OSError(
                        f"Could not read extracted {kind} data in {parts_dir}: {exc}"
                    ) from exc

            yield iter_parts()
            return

    if kind == "ranks" and args.ranks_file.is_file():
        with gzip.open(args.ranks_file, "rb") as f:
            yield f
        return

    name = f"{args.release}-{args.level}-{kind}.txt.gz"
    if args.data_dir:
        path = args.data_dir / name
        if path.is_file():
            with gzip.open(path, "rb") as f:
                yield f
            return

    url = BASE.format(rel=args.release, lvl=args.level, kind=kind)
    req = urllib.request.Request(url, headers={"User-Agent": "backlink-gap/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        with gzip.GzipFile(fileobj=response) as f:
            yield f


def normalize(site, level):
    """Normalize a site or URL to a hostname, removing www for domain-level comparisons."""
    s = site.strip().lower()
    if "//" in s:
        s = urlparse(s).netloc
    s = s.split("/")[0].split(":")[0]
    if level == "domain" and s.startswith("www."):
        s = s[4:]
    return s


def reverse_name(name):
    """Convert a hostname to Common Crawl reversed-label order."""
    return ".".join(reversed(name.split(".")))


def log(msg):
    """Write a timestamped progress message to standard error."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def find_ids(args, wanted):
    """wanted: set of reversed names -> {reversed name: id}"""
    found = {}
    with open_graph_file(args, "vertices") as f:
        for line in f:
            parts = line.rstrip(b"\n").split(b"\t")
            if len(parts) >= 2 and parts[1].decode() in wanted:
                found[parts[1].decode()] = parts[0].decode()
                if len(found) == len(wanted):
                    break
    return found


def scan_edges(args, target_ids):
    """Single pass over edges; returns {target_id: set(source_id)}."""
    suffixes = tuple(b"\t" + t.encode() + b"\n" for t in target_ids)
    by_suffix = {s: t for s, t in zip(suffixes, target_ids)}
    sources = {t: set() for t in target_ids}
    n = 0
    with open_graph_file(args, "edges") as f:
        for line in f:
            n += 1
            if n % 200_000_000 == 0:
                log(f"  edges scanned: {n:,}")
            if line.endswith(suffixes):
                src, dst = line.rstrip(b"\n").split(b"\t")
                sources[dst.decode()].add(src.decode())
    return sources


def resolve_names(args, ids):
    """Map host IDs to their reversed hostnames using the local or remote vertex graph."""
    names = {}
    with open_graph_file(args, "vertices") as f:
        for line in f:
            i, _, rest = line.partition(b"\t")
            if i.decode() in ids:
                names[i.decode()] = rest.split(b"\t")[0].rstrip(b"\r\n").decode()
                if len(names) == len(ids):
                    break
    return names


def load_ranks(args, wanted):
    """Load rank values for the requested reversed hostnames."""
    ranks = {}
    with open_graph_file(args, "ranks") as f:
        for line in f:
            if line.startswith(b"#"):
                continue
            p = line.rstrip(b"\n").split(b"\t")
            name = p[4].decode()
            if name in wanted:
                ranks[name] = (int(p[0]), float(p[1]), int(p[2]), float(p[3]))
                if len(ranks) == len(wanted):
                    break
    return ranks


def main():
    """Parse inputs, find competitor backlink gaps, rank linkers, and write the result CSV."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site", help="your site, e.g. example.com")
    ap.add_argument("competitors", nargs="+", help="1-4 competitor sites")
    ap.add_argument("--release", default="cc-main-2026-jun-jul-aug")
    ap.add_argument("--level", choices=["domain", "host"], default="host")
    ap.add_argument("--vertices-dir", type=Path, default=Path("host_vertices"),
                    help="directory containing extracted vertex .gz parts")
    ap.add_argument("--edges-dir", type=Path, default=Path("host_edges_only"),
                    help="directory containing extracted edge .gz parts")
    ap.add_argument("--ranks-file", type=Path,
                    default=Path("cc-main-2026-jul-aug-sep-host-ranks.txt.gz"),
                    help="local Common Crawl host-ranks file")
    ap.add_argument("--data-dir", type=Path,
                    help="directory with monolithic pre-downloaded graph files")
    ap.add_argument("--min-competitors", type=int, default=1,
                    help="only keep linkers that link to at least this many competitors")
    ap.add_argument("--top", type=int, default=50, help="rows to print (CSV has all)")
    ap.add_argument("--out", default="backlink_gap.csv")
    args = ap.parse_args()

    if not 1 <= len(args.competitors) <= 4:
        ap.error("provide between 1 and 4 competitors")

    site = normalize(args.site, args.level)
    comps = [normalize(c, args.level) for c in args.competitors]
    rev = {n: reverse_name(n) for n in [site] + comps}

    log("Resolving vertex ids...")
    ids = find_ids(args, set(rev.values()))
    missing = [n for n, r in rev.items() if r not in ids]
    if missing:
        sys.exit(f"Not found in the {args.level} graph: {', '.join(missing)}")
    id_of = {n: ids[r] for n, r in rev.items()}

    log("Scanning edges (this is the slow part)...")
    sources = scan_edges(args, list(id_of.values()))
    site_sources = sources[id_of[site]]
    excluded = set(id_of.values())


    gap = {}
    for c in comps:
        for s in sources[id_of[c]] - site_sources - excluded:
            gap.setdefault(s, []).append(c)
    gap = {s: cs for s, cs in gap.items() if len(cs) >= args.min_competitors}
    log(f"Site backlinks: {len(site_sources):,}; gap linkers: {len(gap):,}")
    if not gap:
        return

    log("Resolving names...")
    names = resolve_names(args, set(gap))
    log("Loading Common Crawl ranks...")
    ranks = load_ranks(args, set(names.values()))

    rows = []
    for sid, cs in gap.items():
        rname = names.get(sid)
        if rname is None:
            continue
        hc_pos, hc_val, pr_pos, pr_val = ranks.get(rname, (None, 0.0, None, 0.0))
        rows.append({
            "linking_" + args.level: reverse_name(rname),
            "competitors_linked": len(cs),
            "competitors": " | ".join(sorted(cs)),
            "harmonic_pos": hc_pos,
            "harmonic_val": hc_val,
            "pagerank_pos": pr_pos,
            "pagerank_val": pr_val,
        })
    rows.sort(key=lambda r: (-r["competitors_linked"], -r["harmonic_val"]))

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\nTop {min(args.top, len(rows))} backlink gaps (link to competitors, not to {site}):")
    for r in rows[:args.top]:
        print(f"{r['competitors_linked']}  hc#{str(r['harmonic_pos']):>10}  "
              f"{r['linking_' + args.level]:<40} {r['competitors']}")
    print(f"\nFull results ({len(rows):,} rows) written to {args.out}")


if __name__ == "__main__":
    main()
