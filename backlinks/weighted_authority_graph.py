"""Build the status-weighted edge list used by authority calculations."""

import csv
import os
import sys
import tempfile
from pathlib import Path


OUTPUT_DIR = Path("backlink_index")
ALL_EDGES_CSV = OUTPUT_DIR / "backlink_edges_filtered.csv"
STATUS_CSV = OUTPUT_DIR / "follow_status.csv"
DOFOLLOW_EDGES_CSV = OUTPUT_DIR / "backlink_edges_dofollow.csv"
WEIGHTED_EDGES_CSV = OUTPUT_DIR / "backlink_edges_authority_weighted.csv"
EDGES_CSV = WEIGHTED_EDGES_CSV

STATUS_WEIGHTS = {
    "follow": 1.00,
    "http_403": 0.75,
    "http_429": 0.75,
    "http_500": 0.65,
    "http_502": 0.65,
    "http_503": 0.65,
    "http_504": 0.65,
    "fetch_timeout": 0.60,
    "connection_failed": 0.60,
    "dns_failed": 0.60,
    "ssl_failed": 0.60,
    "not_found_on_homepage": 0.50,
    "http_404": 0.25,
    "http_410": 0.25,
    "nofollow": 0.00,
    "sponsored": 0.00,
    "ugc": 0.00,
}

OUTPUT_FIELDS = [
    "source_domain",
    "target_domain",
    "weight",
    "generation",
    "status_classification",
    "status_weight",
    "backlink_weight",
    "source_cc_harmonic_rank",
    "source_cc_harmonic_score",
    "source_cc_pagerank_rank",
    "source_cc_pagerank_score",
    "target_cc_harmonic_rank",
    "target_cc_harmonic_score",
    "target_cc_pagerank_rank",
    "target_cc_pagerank_score",
]


def classification_weight(classification):
    """Return the configured contribution for a follow-check classification."""
    return STATUS_WEIGHTS.get((classification or "").strip().lower(), 0.0)


def collect_generation_one_edges(edges_csv):
    """Load generation-one edges into a lookup used to weight the authority graph."""
    generation_one = {}
    with open(edges_csv, newline="", encoding="utf-8", buffering=1024 * 1024) as f:
        for row in csv.DictReader(f):
            if (row.get("generation") or "").strip() != "1":
                continue

            source = (row.get("source_domain") or "").strip()
            target = (row.get("target_domain") or "").strip()
            if source and target:
                generation_one[(source, target)] = {
                    "weight": float(row.get("weight") or 1),
                    **{
                        field: row.get(field, "")
                        for field in OUTPUT_FIELDS[7:]
                    },
                }
    return generation_one


def collect_generation_one_statuses(status_csv, generation_one):
    """Collect the best follow-check status for each generation-one edge."""
    best_status = {}
    old_field_limit = csv.field_size_limit()
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    try:
        with open(
            status_csv,
            newline="",
            encoding="utf-8",
            buffering=1024 * 1024,
        ) as f:
            for row in csv.DictReader(f):
                key = (
                    (row.get("source_domain") or "").strip(),
                    (row.get("target_domain") or "").strip(),
                )
                if key not in generation_one:
                    continue

                classification = (
                    row.get("classification") or ""
                ).strip().lower()
                weight = classification_weight(classification)
                current = best_status.get(key)
                if current is None or (weight, classification) > current:
                    best_status[key] = (weight, classification)
    finally:
        csv.field_size_limit(old_field_limit)

    missing = generation_one.keys() - best_status.keys()
    if missing:
        examples = ", ".join(
            f"{source}->{target}"
            for source, target in sorted(missing)[:5]
        )
        raise RuntimeError(
            f"No follow-status rows found for {len(missing)} generation-1 "
            f"edge(s); examples: {examples}"
        )

    return best_status


def build_weighted_authority_edges(
    all_edges_csv=ALL_EDGES_CSV,
    status_csv=STATUS_CSV,
    dofollow_edges_csv=DOFOLLOW_EDGES_CSV,
    output_csv=WEIGHTED_EDGES_CSV,
):
    """Write all weighted Gen 1 edges and only dofollow Gen 2+ edges."""
    for path in (all_edges_csv, status_csv, dofollow_edges_csv):
        if not Path(path).exists():
            raise FileNotFoundError(path)

    generation_one = collect_generation_one_edges(all_edges_csv)
    statuses = collect_generation_one_statuses(status_csv, generation_one)

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    generation_one_count = 0
    later_generation_count = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            buffering=1024 * 1024,
            dir=output_csv.parent,
            prefix=f"{output_csv.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()

            for (source, target), edge in sorted(generation_one.items()):
                backlink_weight = edge["weight"]
                status_weight, classification = statuses[(source, target)]
                writer.writerow(
                    {
                        "source_domain": source,
                        "target_domain": target,
                        "weight": f"{backlink_weight * status_weight:.6f}",
                        "generation": 1,
                        "status_classification": classification,
                        "status_weight": f"{status_weight:.2f}",
                        "backlink_weight": f"{backlink_weight:.6f}",
                        **{
                            field: edge.get(field, "")
                            for field in OUTPUT_FIELDS[7:]
                        },
                    }
                )
                generation_one_count += 1

            with open(
                dofollow_edges_csv,
                newline="",
                encoding="utf-8",
                buffering=1024 * 1024,
            ) as edges_file:
                for row in csv.DictReader(edges_file):
                    try:
                        generation = int(row.get("generation") or 0)
                    except ValueError:
                        continue
                    if generation < 2:
                        continue

                    backlink_weight = float(row.get("weight") or 1)
                    writer.writerow(
                        {
                            "source_domain": row["source_domain"],
                            "target_domain": row["target_domain"],
                            "weight": f"{backlink_weight:.6f}",
                            "generation": generation,
                            "status_classification": "follow",
                            "status_weight": "1.00",
                            "backlink_weight": f"{backlink_weight:.6f}",
                            **{
                                field: row.get(field, "")
                                for field in OUTPUT_FIELDS[7:]
                            },
                        }
                    )
                    later_generation_count += 1

        os.replace(temp_path, output_csv)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    print(
        f"Weighted authority edges written to {output_csv}: "
        f"{generation_one_count:,} generation-1 and "
        f"{later_generation_count:,} generation-2+ dofollow edges"
    )
    return generation_one_count, later_generation_count


def ensure_weighted_authority_edges():
    """Build the weighted edge list when missing or older than its inputs."""
    inputs = (ALL_EDGES_CSV, STATUS_CSV, DOFOLLOW_EDGES_CSV)
    if any(not path.exists() for path in inputs):
        missing = [str(path) for path in inputs if not path.exists()]
        raise FileNotFoundError(
            "Required authority graph input(s) missing: " + ", ".join(missing)
        )

    if not WEIGHTED_EDGES_CSV.exists() or any(
        path.stat().st_mtime > WEIGHTED_EDGES_CSV.stat().st_mtime
        for path in inputs
    ):
        build_weighted_authority_edges()

    return WEIGHTED_EDGES_CSV
