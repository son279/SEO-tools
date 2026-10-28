"""Export Common Crawl host ranks without calculating local authority ranks."""

import csv
from pathlib import Path


INPUT_CSV = Path("backlink_index") / "backlink_edges.csv"
OUTPUT_CSV = Path("backlink_index") / "common_crawl_host_ranks.csv"
RANK_FIELDS = (
    "cc_harmonic_rank",
    "cc_harmonic_score",
    "cc_pagerank_rank",
    "cc_pagerank_score",
)


def export_host_ranks(input_csv=INPUT_CSV, output_csv=OUTPUT_CSV):
    """Copy supplied Common Crawl host ranks into one row per host."""
    hosts = {}
    prefixes = ("source", "target")

    with open(input_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for prefix in prefixes:
                host = (row.get(f"{prefix}_domain") or "").strip()
                if not host:
                    continue
                values = hosts.setdefault(host, {})
                for field in RANK_FIELDS:
                    raw = (row.get(f"{prefix}_{field}") or "").strip()
                    if raw and field not in values:
                        values[field] = raw

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["host_domain", *RANK_FIELDS])
        writer.writeheader()
        for host, values in sorted(hosts.items()):
            writer.writerow({"host_domain": host, **values})

    print(f"Exported {len(hosts):,} Common Crawl host rank rows to {output_csv}")
    return len(hosts)


def main():
    """Validate the backlink edge export exists, then export its Common Crawl host ranks."""
    if not INPUT_CSV.exists():
        raise FileNotFoundError(
            f"{INPUT_CSV} not found. Run backlink_index.py first."
        )
    export_host_ranks()


if __name__ == "__main__":
    main()
