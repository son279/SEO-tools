"""SEO Tools MCP server: on-page crawler + Common Crawl backlink analysis.

Run manually to sanity-check:
    .venv\\Scripts\\python.exe seo_mcp_server.py

Register with Claude Desktop by adding an entry to
`claude_desktop_config.json` (see README section "Claude Desktop MCP setup").

Tools exposed:
    - run_seo_crawl        Crawl a primary site + up to N competitors and
                            build the weighted TF-IDF keyword reports.
    - get_keyword_gap_report  Read back the consolidated JSON handoff file.
    - get_primary_top_keywords  Read the primary site's top discovered terms.
    - get_keyword_gap_csv   Read the competitor keyword-gap rows.
    - get_ai_audit_packet   Read the compact text packet built for LLM audits.
    - list_crawl_outputs    List files/sizes currently in an output dir.
    - get_crawl_status      Poll a background crawl (state, exit code, log).

Backlink tools (backlinks/tools.py, data in BACKLINK_DATA_DIR):
    - backlink_pipeline_status, get/set_backlink_clients
    - start_stage, build_visual_graph, run_backlink_gap_scan,
      job_status, list_backlink_jobs, cancel_job
    - build_index, get_backlink_counts, list_backlinks, backlink_gap,
      get_common_crawl_host_rank, top_common_crawl_hosts,
      get_followup_summary, get_visual_graph
    - list_backlink_reports, read_backlink_report
    - prompts: pipeline_order, backlink_audit, full_seo_and_backlink_audit
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON_EXE = Path(sys.executable)

mcp = FastMCP(
    "seo-tools",
    instructions=(
        "On-page: crawl a primary website and optional competitors with "
        "run_seo_crawl, then inspect the generated SEO, Markdown and keyword-gap "
        "reports (run the crawl before requesting its reports). "
        "Off-page: backlink analysis on the Common Crawl host graph; call "
        "backlink_pipeline_status first to see which data and stages are ready."
    ),
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _resolve_output_dir(output_dir: str) -> Path:
    path = Path(output_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@mcp.tool()
async def run_seo_crawl(
        primary: str,
        competitors: Annotated[list[str] | None, Field(max_length=4)] = None,
        output_dir: str = "seo_output",
        threads: Annotated[int, Field(ge=1, le=32)] = 8,
        delay: Annotated[float, Field(ge=0)] = 0.25,
        max_ngram: Annotated[int, Field(ge=1, le=3)] = 3,
        min_df: Annotated[int, Field(ge=1)] = 2,
        top_terms: Annotated[int, Field(ge=1)] = 150,
        timeout_seconds: Annotated[int, Field(ge=1)] = 1800,
        background: bool = True,
) -> dict[str, Any]:
    """Crawl a primary website and up to 4 competitors, then build the
    weighted TF-IDF keyword reports (including keyword_gap_report.json).

    Args:
        primary: Primary website or sitemap URL.
        competitors: List of competitor website/sitemap URLs (0-4 typical).
        output_dir: Directory (relative to the project folder, or absolute)
            to write results into. Reused/overwritten on each run.
        threads: Crawler worker threads (1-32).
        delay: Minimum request delay per domain, in seconds.
        max_ngram: Maximum keyword phrase size (1, 2, or 3 words).
        min_df: Minimum number of pages a term must appear on to be counted.
        top_terms: Max rows kept in the root keyword reports.
        timeout_seconds: Hard timeout for the whole crawl before this tool
            gives up and reports a timeout error (foreground mode only).
        background: If True (default), start the crawl as a detached
            process and return immediately. Progress goes to
            <output_dir>/crawl.log and the exit code to
            <output_dir>/crawl_status.json. Poll with get_crawl_status.
            Use this when the client has a short per-call timeout.

    Returns:
        A dict with success flag, resolved output directory, the tail of the
        console log, and the list of output files produced.
    """
    competitors = competitors or []
    resolved_output = _resolve_output_dir(output_dir)

    cmd = [
        str(PYTHON_EXE),
        str(PROJECT_ROOT / "cli.py"),
        _normalize_url(primary),
        *[_normalize_url(c) for c in competitors],
        "--output", str(resolved_output),
        "--threads", str(threads),
        "--delay", str(delay),
        "--max-ngram", str(max_ngram),
        "--min-df", str(min_df),
        "--top-terms", str(top_terms),
    ]

    if background:
        return _start_background_crawl(cmd, resolved_output)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        log = stdout.decode("utf-8", errors="replace")
        success = proc.returncode == 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "success": False,
            "error": f"Crawl timed out after {timeout_seconds}s",
            "output_dir": str(resolved_output),
        }
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not start the crawler: {exc}",
            "output_dir": str(resolved_output),
        }

    output_files = []
    if resolved_output.exists():
        output_files = sorted(
            str(p.relative_to(resolved_output))
            for p in resolved_output.rglob("*")
            if p.is_file()
        )

    return {
        "success": success,
        "return_code": proc.returncode,
        "output_dir": str(resolved_output),
        "log_tail": "\n".join(log.splitlines()[-60:]),
        "output_files": output_files,
        "keyword_gap_report_available": (resolved_output / "keyword_gap_report.json").exists(),
    }


_BG_WRAPPER = (
    "import json, subprocess, sys, time\n"
    "status_path, log_path = sys.argv[1], sys.argv[2]\n"
    "cmd = sys.argv[3:]\n"
    "start = time.time()\n"
    "with open(log_path, 'w', encoding='utf-8', errors='replace') as log:\n"
    "    rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT)\n"
    "with open(status_path, 'w', encoding='utf-8') as f:\n"
    "    json.dump({'state': 'finished', 'return_code': rc,\n"
    "               'elapsed_seconds': round(time.time() - start, 1)}, f)\n"
)


def _start_background_crawl(cmd: list[str], resolved_output: Path) -> dict[str, Any]:
    """Launch the crawl detached so it survives short client timeouts."""
    import subprocess
    import time

    resolved_output.mkdir(parents=True, exist_ok=True)
    status_path = resolved_output / "crawl_status.json"
    log_path = resolved_output / "crawl.log"
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump({"state": "running", "started_at": time.time()}, f)

    # -u keeps the crawler's output unbuffered so crawl.log updates live.
    child_cmd = [cmd[0], "-u", *cmd[1:]]
    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [str(PYTHON_EXE), "-c", _BG_WRAPPER, str(status_path), str(log_path), *child_cmd],
        **kwargs,
    )
    return {
        "started": True,
        "background": True,
        "pid": proc.pid,
        "output_dir": str(resolved_output),
        "log_file": str(log_path),
        "next_step": "Poll get_crawl_status(output_dir) until state == 'finished'.",
    }


@mcp.tool()
def get_crawl_status(
        output_dir: str = "seo_output",
        log_lines: Annotated[int, Field(ge=1, le=500)] = 25,
) -> dict[str, Any]:
    """Check a background crawl started by run_seo_crawl: running/finished
    state, exit code, and the tail of crawl.log."""
    resolved_output = _resolve_output_dir(output_dir)
    status_path = resolved_output / "crawl_status.json"
    log_path = resolved_output / "crawl.log"
    if not status_path.exists():
        return {"error": f"No crawl_status.json in {resolved_output}"}
    with open(status_path, "r", encoding="utf-8") as f:
        status = json.load(f)
    tail = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-log_lines:])
    status.update({
        "output_dir": str(resolved_output),
        "log_tail": tail,
        "keyword_gap_report_available": (resolved_output / "keyword_gap_report.json").exists(),
    })
    return status


@mcp.tool()
def get_keyword_gap_report(
        output_dir: str = "seo_output",
        max_rows_per_section: Annotated[int, Field(ge=1, le=500)] = 40,
) -> dict[str, Any]:
    """Read back the consolidated keyword_gap_report.json produced by
    run_seo_crawl. This is the primary file to filter/analyze for keyword
    opportunities.

    Args:
        output_dir: Directory used for the crawl (relative or absolute).
        max_rows_per_section: Cap on how many rows are returned per keyword
            list, to keep the payload manageable.
    """
    resolved_output = _resolve_output_dir(output_dir)
    report_path = resolved_output / "keyword_gap_report.json"
    if not report_path.exists():
        return {"error": f"No keyword_gap_report.json found in {resolved_output}"}

    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    for key in ("primary_top_keywords", "missing_keywords", "keywords_to_strengthen", "keyword_gap_full"):
        if key in report and isinstance(report[key], list):
            report[key] = report[key][:max_rows_per_section]

    return report


def _read_csv_rows(path: Path, max_rows: int) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
        return rows


@mcp.tool()
def get_primary_top_keywords(
        output_dir: str = "seo_output",
        max_rows: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Read the primary site's top discovered TF-IDF keywords/phrases."""
    resolved_output = _resolve_output_dir(output_dir)
    path = resolved_output / "keyword_reports" / "primary_top_keywords.csv"
    if not path.exists():
        path = resolved_output / "primary_top_keywords.csv"  # legacy location
    if not path.exists():
        return {"error": f"No primary_top_keywords.csv found in {resolved_output}"}
    return {"rows": _read_csv_rows(path, max_rows)}


@mcp.tool()
def get_keyword_gap_csv(
        output_dir: str = "seo_output",
        max_rows: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Read the competitor keyword-gap rows (terms competitors emphasize
    more than the primary site)."""
    resolved_output = _resolve_output_dir(output_dir)
    path = resolved_output / "keyword_reports" / "tfidf_keyword_gap.csv"
    if not path.exists():
        path = resolved_output / "tfidf_keyword_gap.csv"  # legacy location
    if not path.exists():
        return {"error": f"No tfidf_keyword_gap.csv found in {resolved_output}"}
    return {"rows": _read_csv_rows(path, max_rows)}


@mcp.tool()
def get_ai_audit_packet(output_dir: str = "seo_output") -> dict[str, Any]:
    """Read the compact ai_audit_packet.txt built for LLM SEO audits."""
    resolved_output = _resolve_output_dir(output_dir)
    path = resolved_output / "ai_audit_packet.txt"
    if not path.exists():
        return {"error": f"No ai_audit_packet.txt found in {resolved_output}"}
    return {"packet": path.read_text(encoding="utf-8")}


@mcp.tool()
def list_crawl_outputs(output_dir: str = "seo_output") -> dict[str, Any]:
    """List files and sizes currently present in a crawl output directory."""
    resolved_output = _resolve_output_dir(output_dir)
    if not resolved_output.exists():
        return {"error": f"{resolved_output} does not exist"}

    files = []
    for p in sorted(resolved_output.rglob("*")):
        if p.is_file():
            files.append({
                "path": str(p.relative_to(resolved_output)),
                "bytes": p.stat().st_size,
            })
    return {"output_dir": str(resolved_output), "files": files}


# Backlink tools (pipeline stages, queries, reports, prompts).
sys.path.insert(0, str(PROJECT_ROOT))
from backlinks.tools import register as _register_backlink_tools  # noqa: E402

_register_backlink_tools(mcp)


def main() -> None:
    """Run over stdio (default, for Claude Desktop) or with --http for the LAN.

    LAN example:  python seo_mcp_server.py --http --port 8765 --token <secret>
    """
    from lan_http import run_from_cli
    run_from_cli(mcp, default_port=8765)


if __name__ == "__main__":
    main()
