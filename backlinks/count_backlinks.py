"""Count direct backlinks for every domain listed in clients.txt.

Reads backlink_index/backlink_edges.csv (generation-1 edges only) and writes
backlink_index/first_gen_backlink_counts.csv, also printing a table.
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

# Data folder = working directory (the MCP server runs stages from BACKLINK_DATA_DIR).
BASE = Path.cwd()
LINKS_FILE = BASE / "clients.txt"
EDGES_FILE = BASE / "backlink_index" / "backlink_edges.csv"
OUTPUT_FILE = BASE / "backlink_index" / "clients_backlink_counts.csv"

csv.field_size_limit(sys.maxsize)


def reverse_host(host):
    """Convert a hostname to Common Crawl reversed-label order."""
    return ".".join(reversed(host.split(".")))


def normalize(link):
    """Normalize an input link or hostname for backlink matching."""
    link = link.strip().lower()
    for prefix in ("https://", "http://"):
        if link.startswith(prefix):
            link = link[len(prefix):]
    link = link.split("/")[0]
    return link[4:] if link.startswith("www.") else link


def match_link(target, target_to_link):

    """Count links matching a target host and its applicable subdomains."""
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        link = target_to_link.get(".".join(parts[:i]))
        if link:
            return link
    return None


def load_links():
    """Read and normalize the configured list of links."""
    with open(LINKS_FILE, encoding="utf-8") as f:
        links = [normalize(line) for line in f if line.strip()]
    return list(dict.fromkeys(links))


def main():
    """Count matching backlinks for the configured targets and report the results."""
    links = load_links()
    target_to_link = {reverse_host(link): link for link in links}

    edge_counts = defaultdict(int)
    sources = defaultdict(set)

    with open(EDGES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            link = match_link(row["target_domain"], target_to_link)
            if link is None:
                continue
            edge_counts[link] += 1
            sources[link].add(row["source_id"])

    rows = [(link, edge_counts[link], len(sources[link])) for link in links]

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["domain", "backlink_count", "unique_referring_hosts"])
        writer.writerows(rows)

    width = max(len(r[0]) for r in rows)
    print(f"{'domain':<{width}}  backlinks  referring_hosts")
    for link, edges, hosts in rows:
        print(f"{link:<{width}}  {edges:>9}  {hosts:>15}")
    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
