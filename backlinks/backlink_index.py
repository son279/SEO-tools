import gzip
import csv
import os
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed















CLIENTS_FILE = Path("clients.txt")

VERTEX_DIR = Path("host_vertices")
EDGE_DIR = Path("host_edges_only")
OUTPUT_DIR = Path("backlink_index")
HOST_RANKS_FILE = Path("cc-main-2026-jul-aug-sep-host-ranks.txt.gz")


MAX_GENERATIONS = 4



FRONTIER_CAP = 5000


MAX_EDGE_FILES = None
MAX_VERTEX_FILES = None




MAX_WORKERS = min(8, max(2, os.cpu_count() or 4))






def load_target_domains(path):
    """
    Load client/root domains from a text file.

    Expected format:
        example.com
        www.example2.com

    Blank lines and lines starting with # are ignored.

    Basic URL forms are also accepted:
        https://example.com/
        http://www.example2.com/path

    They are normalized to host names.
    """
    if not path.exists():
        print(f"ERROR: client file not found: {path}")
        return []

    domains = []
    seen = set()

    with open(path, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            value = raw_line.strip()

            if not value or value.startswith("#"):
                continue


            value = value.split("#", 1)[0].strip()


            value = value.lower()
            value = value.removeprefix("https://")
            value = value.removeprefix("http://")
            value = value.split("/", 1)[0]
            value = value.split("?", 1)[0]
            value = value.split("#", 1)[0]
            value = value.rstrip(".")


            if ":" in value and value.count(":") == 1:
                host, port = value.rsplit(":", 1)
                if port.isdigit():
                    value = host

            if not value or "." not in value:
                print(f"  [SKIP] Invalid client entry: {raw_line.strip()!r}")
                continue

            if value not in seen:
                seen.add(value)
                domains.append(value)

    return domains


def reverse_domain(domain):
    """'example.com' -> 'com.example' (Common Crawl host ordering)."""
    return ".".join(reversed(domain.strip().lower().split(".")))


def vertex_files():
    """Return sorted local vertex parts, applying the optional file limit."""
    files = sorted(VERTEX_DIR.glob("*.gz"))
    return files[:MAX_VERTEX_FILES] if MAX_VERTEX_FILES else files


def edge_files():
    """Return sorted local edge parts, applying the optional file limit."""
    files = sorted(EDGE_DIR.glob("*.gz"))
    return files[:MAX_EDGE_FILES] if MAX_EDGE_FILES else files


def iter_vertex_file(path):
    """Yield (host_id, reversed_host) rows from one vertex .gz file."""
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue

            try:
                host_id = int(parts[0])
            except ValueError:
                continue

            yield host_id, parts[1]


def iter_edge_file(path):
    """Yield (src_id, dst_id) rows from one edge .gz file."""
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue

            try:
                src = int(parts[0])
                dst = int(parts[1])
            except ValueError:
                continue

            yield src, dst


def load_host_ranks(hosts, path=HOST_RANKS_FILE):
    """Load Common Crawl rank records only for hosts in the backlink graph."""
    if not path.exists():
        raise FileNotFoundError(f"Common Crawl host-ranks file not found: {path}")

    wanted = set(hosts)
    requested_count = len(wanted)
    ranks = {}

    with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        required_fields = {
            "#harmonicc_pos",
            "#harmonicc_val",
            "#pr_pos",
            "#pr_val",
            "#host_rev",
        }
        if not required_fields.issubset(reader.fieldnames or ()):
            raise ValueError(
                f"Unexpected Common Crawl host-ranks schema in {path}"
            )

        for row in reader:
            host = (row.get("#host_rev") or "").strip()
            if host not in wanted:
                continue

            ranks[host] = {
                "harmonic_rank": row["#harmonicc_pos"],
                "harmonic_score": row["#harmonicc_val"],
                "pagerank_rank": row["#pr_pos"],
                "pagerank_score": row["#pr_val"],
            }
            wanted.remove(host)
            if not wanted:
                break

    print(
        f"Common Crawl host ranks matched: "
        f"{len(ranks):,}/{requested_count:,}"
    )
    if wanted:
        print(f"  Hosts without a Common Crawl rank: {len(wanted):,}")
    return ranks






def _scan_vertex_file_for_targets(path, reversed_targets):
    """
    Worker function.

    Scans one vertex file and returns:
      local_target_ids: original domain -> set(host IDs)
      local_id_to_domain: host ID -> reversed host
    """
    local_target_ids = defaultdict(set)
    local_id_to_domain = {}

    for host_id, reversed_host in iter_vertex_file(path):
        for rev_target, original_domain in reversed_targets.items():
            if (
                reversed_host == rev_target
                or reversed_host.startswith(rev_target + ".")
            ):
                local_target_ids[original_domain].add(host_id)
                local_id_to_domain[host_id] = reversed_host

    return local_target_ids, local_id_to_domain


def resolve_target_ids(target_domains):
    """
    Match each target domain and its subdomains to Common Crawl vertex IDs.

    Vertex files are scanned concurrently.
    """
    reversed_targets = {
        reverse_domain(domain): domain
        for domain in target_domains
    }

    target_ids = defaultdict(set)
    id_to_domain = {}

    files = vertex_files()

    print("\nSTEP 1: Resolving target domains to host IDs...")
    print(f"  Vertex files: {len(files):,}")
    print(f"  Worker threads: {MAX_WORKERS}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _scan_vertex_file_for_targets,
                path,
                reversed_targets,
            ): path
            for path in files
        }

        completed = 0

        for future in as_completed(futures):
            path = futures[future]
            completed += 1

            try:
                local_targets, local_domains = future.result()
            except Exception as exc:
                print(f"  [ERROR] {path.name}: {exc}")
                continue

            for domain, ids in local_targets.items():
                target_ids[domain].update(ids)

            id_to_domain.update(local_domains)

            print(
                f"\r  [vertices] {completed}/{len(files)} completed",
                end="",
                flush=True,
            )

    print()

    for domain in target_domains:
        n = len(target_ids.get(domain, ()))
        print(f"  {domain!r}: {n} matching host id(s)")

    return target_ids, id_to_domain






def _scan_edge_file_for_frontier(path, frontier):
    """
    Worker function.

    Scan one edge .gz file and find edges whose destination is
    in the current BFS frontier.

    Returns:
      matched: list[(src, dst)]
      counts: dict[src] -> number of matching edges
    """
    matched = []
    counts = defaultdict(int)


    for src, dst in iter_edge_file(path):
        if dst in frontier:
            matched.append((src, dst))
            counts[src] += 1

    return matched, counts


def build_backlink_generations(all_target_ids):
    """
    Reverse BFS over Common Crawl host edges.

    Each generation scans all edge part files concurrently.

    Returns:
      collected_edges: list[(src_id, dst_id, generation)]
      all_ids_seen: set of targets + backlinking hosts followed by BFS
    """
    visited_ids = set(all_target_ids)
    frontier = set(all_target_ids)
    collected_edges = []

    files = edge_files()

    print(f"\nEdge files available: {len(files):,}")
    print(f"Worker threads: {MAX_WORKERS}")

    for gen in range(1, MAX_GENERATIONS + 1):
        if not frontier:
            print(
                f"\nGeneration {gen}: frontier is empty, stopping early."
            )
            break

        print(
            f"\nSTEP 2: Generation {gen} - scanning edges "
            f"(frontier size = {len(frontier):,})..."
        )

        counts = defaultdict(int)
        matched = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _scan_edge_file_for_frontier,
                    path,
                    frontier,
                ): path
                for path in files
            }

            completed = 0

            for future in as_completed(futures):
                path = futures[future]
                completed += 1

                try:
                    local_matched, local_counts = future.result()
                except Exception as exc:
                    print(f"\n  [ERROR] {path.name}: {exc}")
                    continue

                matched.extend(local_matched)

                for src, count in local_counts.items():
                    counts[src] += count

                print(
                    f"\r  [edges] {completed}/{len(files)} completed "
                    f"| matches so far: {len(matched):,}",
                    end="",
                    flush=True,
                )

        print()

        print(
            f"  Generation {gen}: {len(matched):,} matching edges, "
            f"{len(counts):,} distinct source hosts found."
        )


        candidates = [
            src
            for src in counts
            if src not in visited_ids
        ]

        if (
            FRONTIER_CAP is not None
            and len(candidates) > FRONTIER_CAP
        ):
            candidates.sort(
                key=lambda src: counts[src],
                reverse=True,
            )

            dropped = len(candidates) - FRONTIER_CAP
            candidates = candidates[:FRONTIER_CAP]

            print(
                f"  Frontier capping: kept top {FRONTIER_CAP:,} sources "
                f"by link count, dropped {dropped:,}."
            )

        next_frontier = set(candidates)

        collected_edges.extend(
            (src, dst, gen)
            for src, dst in matched
        )

        visited_ids.update(next_frontier)
        frontier = next_frontier

    return collected_edges, visited_ids






def _scan_vertex_file_for_ids(path, needed_ids):
    """
    Worker function.

    Resolve only host IDs that appear in needed_ids.
    """
    found = {}

    for host_id, reversed_host in iter_vertex_file(path):
        if host_id in needed_ids:
            found[host_id] = reversed_host

    return found


def resolve_ids_to_domains(needed_ids, already_known):
    """
    Resolve backlink host IDs back to Common Crawl reversed domain names.

    Vertex part files are scanned concurrently.
    """
    id_to_domain = dict(already_known)
    still_needed = set(needed_ids) - set(id_to_domain)

    if not still_needed:
        return id_to_domain

    files = vertex_files()

    print(
        f"\nSTEP 3: Resolving {len(still_needed):,} "
        f"host IDs to domain names..."
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _scan_vertex_file_for_ids,
                path,
                still_needed,
            ): path
            for path in files
        }

        completed = 0

        for future in as_completed(futures):
            path = futures[future]
            completed += 1

            try:
                local_found = future.result()
            except Exception as exc:
                print(f"\n  [ERROR] {path.name}: {exc}")
                continue

            id_to_domain.update(local_found)

            print(
                f"\r  [vertices] {completed}/{len(files)} completed "
                f"| resolved so far: "
                f"{len(set(local_found) | (set(id_to_domain) & still_needed)):,}"
                f"/{len(still_needed):,}",
                end="",
                flush=True,
            )

    print()

    unresolved = still_needed - set(id_to_domain)

    if unresolved:
        print(
            f"  WARNING: {len(unresolved):,} host IDs "
            f"had no matching vertex row."
        )

    return id_to_domain






def write_output(
    collected_edges,
    id_to_domain,
    target_ids_by_domain,
    host_ranks,
):
    """Write collected backlink edges with host ranks and generation summaries to CSV files."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    edges_path = OUTPUT_DIR / "backlink_edges.csv"
    summary_path = OUTPUT_DIR / "generation_summary.csv"

    print(
        f"\nSTEP 4: Writing {len(collected_edges):,} "
        f"edges to {edges_path}"
    )

    with open(
        edges_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.writer(f)

        writer.writerow([
            "generation",
            "source_id",
            "source_domain",
            "target_id",
            "target_domain",
            "source_cc_harmonic_rank",
            "source_cc_harmonic_score",
            "source_cc_pagerank_rank",
            "source_cc_pagerank_score",
            "target_cc_harmonic_rank",
            "target_cc_harmonic_score",
            "target_cc_pagerank_rank",
            "target_cc_pagerank_score",
        ])

        for src, dst, gen in collected_edges:
            source_domain = id_to_domain.get(src, "")
            target_domain = id_to_domain.get(dst, "")
            source_rank = host_ranks.get(source_domain, {})
            target_rank = host_ranks.get(target_domain, {})
            writer.writerow([
                gen,
                src,
                source_domain,
                dst,
                target_domain,
                source_rank.get("harmonic_rank", ""),
                source_rank.get("harmonic_score", ""),
                source_rank.get("pagerank_rank", ""),
                source_rank.get("pagerank_score", ""),
                target_rank.get("harmonic_rank", ""),
                target_rank.get("harmonic_score", ""),
                target_rank.get("pagerank_rank", ""),
                target_rank.get("pagerank_score", ""),
            ])

    gen_counts = defaultdict(lambda: [0, set()])

    for src, dst, gen in collected_edges:
        gen_counts[gen][0] += 1
        gen_counts[gen][1].add(src)

    with open(
        summary_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.writer(f)

        writer.writerow([
            "generation",
            "edge_count",
            "distinct_source_hosts",
        ])

        for gen in sorted(gen_counts):
            count, srcs = gen_counts[gen]
            writer.writerow([
                gen,
                count,
                len(srcs),
            ])

    print(f"Summary written to {summary_path}")

    per_target_path = (
        OUTPUT_DIR / "gen1_backlink_counts_by_target.csv"
    )

    gen1_counts = defaultdict(int)

    for src, dst, gen in collected_edges:
        if gen == 1:
            gen1_counts[dst] += 1

    with open(
        per_target_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.writer(f)

        writer.writerow([
            "domain",
            "target_id",
            "direct_backlink_count",
        ])

        for domain, ids in target_ids_by_domain.items():
            for host_id in ids:
                writer.writerow([
                    domain,
                    host_id,
                    gen1_counts.get(host_id, 0),
                ])

    print(
        "Per-target Gen-1 backlink counts written to "
        f"{per_target_path}"
    )






def main():
    """Run the backlink-index workflow for the configured client domains."""
    domains = load_target_domains(CLIENTS_FILE)

    if not domains:
        print(
            f"ERROR: no valid client domains were loaded from {CLIENTS_FILE}."
        )
        return

    print(f"Loaded {len(domains):,} client domain(s) from {CLIENTS_FILE}.")
    for domain in domains:
        print(f"  - {domain}")

    print(f"Using {MAX_WORKERS} worker threads.")

    target_ids_by_domain, target_id_to_domain = (
        resolve_target_ids(domains)
    )

    all_target_ids = set()

    for ids in target_ids_by_domain.values():
        all_target_ids.update(ids)

    if not all_target_ids:
        print(
            "\nERROR: none of the target domains matched any vertex. "
            "Check spelling / TLDs and try again."
        )
        return

    collected_edges, all_ids_seen = (
        build_backlink_generations(all_target_ids)
    )

    needed_ids = set(all_target_ids)

    for src, dst, _gen in collected_edges:
        needed_ids.add(src)
        needed_ids.add(dst)

    id_to_domain = resolve_ids_to_domains(
        needed_ids,
        target_id_to_domain,
    )

    host_ranks = load_host_ranks(id_to_domain.values())

    write_output(
        collected_edges,
        id_to_domain,
        target_ids_by_domain,
        host_ranks,
    )

    print("\nDone.")
    print(
        "Total unique hosts discovered "
        f"(targets + backlinkers): {len(all_ids_seen):,}"
    )
    print(
        "Total backlink edges collected: "
        f"{len(collected_edges):,}"
    )


if __name__ == "__main__":
    main()
