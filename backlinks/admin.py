"""Housekeeping helpers for the backlink tools: data-folder status, client list, reports."""
import csv
import itertools
import os
import sys

from . import config, jobs, queries

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Stage order used to suggest the next step.
PIPELINE = ["extract", "backlink_index", "graph_filter", "followup_check", "visual_graph"]
STAGE_OUTPUTS = {
    "extract": ["host_vertices", "host_edges_only"],
    "backlink_index": ["backlink_index/backlink_edges.csv"],
    "graph_filter": [config.FILTERED_EDGES_ANY],
    "followup_check": [config.FOLLOWUP_EDGES_ANY],
    "visual_graph": ["backlink_index/visual_graph_edges.csv"],
}


def _size(path):
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return 0


def _mb(n):
    return round(n / 1_048_576, 1)


def read_clients():
    if not config.CLIENTS_FILE.exists():
        return []
    hosts = []
    for line in config.CLIENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            hosts.append(line)
    return hosts


def set_clients(hosts, mode="replace"):
    if mode not in ("replace", "add", "remove"):
        raise ValueError("mode must be replace, add or remove.")
    new = []
    for h in hosts:
        h = queries.normalize_host(h)
        if h not in new:
            new.append(h)
    current = read_clients()
    if mode == "replace":
        result = new
    elif mode == "add":
        result = current + [h for h in new if h not in current]
    else:
        result = [h for h in current if h not in new]
    config.ROOT.mkdir(parents=True, exist_ok=True)
    config.CLIENTS_FILE.write_text("".join(f"{h}\n" for h in result), encoding="utf-8")
    return {"clients_file": str(config.CLIENTS_FILE), "clients": result,
            "note": "Re-run stage backlink_index (and later stages) for changes to take effect."}


def pipeline_status():
    root = config.ROOT
    files = {}
    if config.INDEX_DIR.exists():
        for p in sorted(config.INDEX_DIR.iterdir()):
            if p.is_file():
                files[p.name] = _mb(p.stat().st_size)
    stages = []
    for name in config.STAGES:
        missing = jobs.missing_inputs(name)
        outputs = STAGE_OUTPUTS.get(name, [])
        stages.append({
            "stage": name,
            "about": config.STAGE_HELP.get(name, ""),
            "ready": not missing,
            "missing_inputs": missing,
            "done": bool(outputs) and all(jobs._exists(o) for o in outputs),
        })
    done = {s["stage"] for s in stages if s["done"]}
    next_stage = next((s for s in PIPELINE if s not in done), None)
    if next_stage is None:
        next_step = "build_index" if not config.QUERY_DB.exists() else "query tools are ready"
    else:
        next_step = f"start_stage('{next_stage}')"
    busy = jobs.running_job()
    return {
        "data_dir": str(root),
        "data_dir_exists": root.exists(),
        "data_dir_source": "BACKLINK_DATA_DIR" if os.environ.get("BACKLINK_DATA_DIR") else "default (<repo>/backlink_data)",
        "clients": read_clients(),
        "inputs_mb": {
            "host_vertices": _mb(_size(root / "host_vertices")),
            "host_edges_only": _mb(_size(root / "host_edges_only")),
        },
        "query_index_built": config.QUERY_DB.exists(),
        "backlink_index_files_mb": files,
        "stages": stages,
        "running_job": busy and {"job_id": busy["id"], "stage": busy["stage"]},
        "suggested_next_step": next_step,
    }


def list_reports():
    out = []
    for name, filename in config.REPORTS.items():
        p = config.INDEX_DIR / filename
        out.append({"report": name, "file": filename, "exists": p.exists(),
                    "mb": _mb(p.stat().st_size) if p.exists() else None})
    return out


def read_report(name, limit=100, offset=0, contains=None):
    if name not in config.REPORTS:
        raise ValueError(f"Unknown report {name!r}. Valid: {sorted(config.REPORTS)}")
    path = config.INDEX_DIR / config.REPORTS[name]
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found in {config.INDEX_DIR}; run the stage that writes it.")
    limit = max(1, min(int(limit), config.MAX_ROWS))
    offset = max(0, int(offset))
    needle = contains.lower() if contains else None
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = reader if needle is None else (
            r for r in reader if any(needle in (v or "").lower() for v in r.values()))
        page = list(itertools.islice(rows, offset, offset + limit + 1))
        columns = reader.fieldnames
    return {
        "report": name,
        "file": str(path),
        "columns": columns,
        "offset": offset,
        "rows": page[:limit],
        "has_more": len(page) > limit,
    }
