import gzip
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests







VERTEX_PATHS_FILE = "cc-main-2026-jun-jul-aug-host-vertices.paths.gz"
EDGE_PATHS_FILE = "cc-main-2026-jun-jul-aug-host-edges.paths.gz"

BASE_URL = "https://data.commoncrawl.org/"
HOST_RANKS_RELEASE = "cc-main-2026-jul-aug-sep"
HOST_RANKS_PATH = (
    f"projects/hyperlinkgraph/{HOST_RANKS_RELEASE}/host/"
    f"{HOST_RANKS_RELEASE}-host-ranks.txt.gz"
)


VERTEX_OUTPUT_DIR = Path("host_vertices")
EDGE_OUTPUT_DIR = Path("host_edges_only")
HOST_RANKS_OUTPUT_DIR = Path(".")


VERTEX_FILTERED_PATHS_FILE = "vertices_only_paths.txt"
EDGE_FILTERED_PATHS_FILE = "edges_only_paths.txt"

EXTRACT_DOWNLOADED_FILES = False

CHUNK_SIZE = 1024 * 1024
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
RETRY_DELAY = 5


MAX_WORKERS = 8


MAX_VERTEX_FILES = None
MAX_EDGE_FILES = None

_thread_local = threading.local()






def get_session():
    """Give each download worker its own requests.Session."""
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update(
            {"User-Agent": "CommonCrawlBacklinkResearch/2.1"}
        )
        _thread_local.session = session

    return _thread_local.session






def read_paths_file(paths_file, dataset_type):
    """
    Read every non-empty path from a Common Crawl manifest.

    IMPORTANT:
    Do NOT try to classify the individual paths by searching for
    'host-vertices' or 'host-edges'.

    The manifest filename itself already tells us which kind of
    files its entries represent.
    """
    paths_file = Path(paths_file)

    if not paths_file.exists():
        raise FileNotFoundError(
            f"{dataset_type} manifest not found: {paths_file.resolve()}"
        )

    paths = []

    print(f"\nReading {dataset_type} manifest: {paths_file}")

    with gzip.open(
        paths_file,
        "rt",
        encoding="utf-8",
        errors="ignore",
    ) as f:
        for line in f:
            object_path = line.strip()

            if object_path:
                paths.append(object_path)

    print(f"  Found: {len(paths):,} paths")


    if dataset_type == "vertices":
        print("  These paths will be treated as VERTEX files.")
    elif dataset_type == "edges":
        print("  These paths will be treated as EDGE files.")

    return paths


def write_paths_file(paths, output_path):
    """Write a readable copy of the paths that will be downloaded."""
    output_path = Path(output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        for object_path in paths:
            f.write(object_path + "\n")

    print(
        f"Path list saved: {output_path.resolve()} "
        f"({len(paths):,} entries)"
    )






def download_file(object_path, output_dir, dataset_type):
    """
    Download one graph part.

    Vertex jobs are always written to host_vertices/.
    Edge jobs are always written to host_edges_only/.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = os.path.basename(object_path)
    output_path = output_dir / filename
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    url = BASE_URL + object_path


    if output_path.exists() and output_path.stat().st_size > 0:
        return {
            "type": dataset_type,
            "status": "skipped",
            "path": output_path,
            "url": url,
            "size": output_path.stat().st_size,
        }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            session = get_session()

            with session.get(
                url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()

                expected_size = int(
                    response.headers.get("content-length", 0)
                )
                downloaded = 0

                with open(temp_path, "wb") as out:
                    for chunk in response.iter_content(
                        chunk_size=CHUNK_SIZE
                    ):
                        if not chunk:
                            continue

                        out.write(chunk)
                        downloaded += len(chunk)

            if expected_size and downloaded != expected_size:
                raise OSError(
                    f"size mismatch: expected {expected_size:,}, "
                    f"got {downloaded:,} bytes"
                )

            temp_path.replace(output_path)

            if EXTRACT_DOWNLOADED_FILES:
                extract_gzip_file(output_path)

            return {
                "type": dataset_type,
                "status": "downloaded",
                "path": output_path,
                "url": url,
                "size": output_path.stat().st_size,
            }

        except (requests.RequestException, OSError) as exc:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                return {
                    "type": dataset_type,
                    "status": "failed",
                    "path": output_path,
                    "url": url,
                    "size": 0,
                    "error": str(exc),
                }






def extract_gzip_file(gz_path):
    """Decompress a gzip file beside its source unless a completed output already exists."""
    gz_path = Path(gz_path)

    if gz_path.suffix != ".gz":
        return

    output_path = gz_path.with_suffix("")

    if output_path.exists() and output_path.stat().st_size > 0:
        return

    with gzip.open(gz_path, "rb") as source:
        with open(output_path, "wb") as destination:
            shutil.copyfileobj(source, destination)






def preview_gzip_file(gz_path, number_of_lines=5):
    """Print a limited number of decoded lines from a gzip file."""
    print(f"\nPreviewing: {gz_path}")

    with gzip.open(
        gz_path,
        "rt",
        encoding="utf-8",
        errors="ignore",
    ) as f:
        for i, line in enumerate(f):
            print("  " + line.rstrip())

            if i + 1 >= number_of_lines:
                break






def main():
    """Read graph manifests and download their vertex, edge, and host-rank files."""
    print("=" * 72)
    print("COMMON CRAWL HOST GRAPH PARALLEL DOWNLOADER")
    print("=" * 72)

    try:

        vertex_paths = read_paths_file(
            VERTEX_PATHS_FILE,
            "vertices",
        )

        edge_paths = read_paths_file(
            EDGE_PATHS_FILE,
            "edges",
        )

    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}")
        return

    if MAX_VERTEX_FILES is not None:
        vertex_paths = vertex_paths[:MAX_VERTEX_FILES]

    if MAX_EDGE_FILES is not None:
        edge_paths = edge_paths[:MAX_EDGE_FILES]

    write_paths_file(
        vertex_paths,
        VERTEX_FILTERED_PATHS_FILE,
    )

    write_paths_file(
        edge_paths,
        EDGE_FILTERED_PATHS_FILE,
    )

    VERTEX_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    EDGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


    jobs = []

    for object_path in vertex_paths:
        jobs.append(
            (object_path, VERTEX_OUTPUT_DIR, "vertices")
        )

    for object_path in edge_paths:
        jobs.append(
            (object_path, EDGE_OUTPUT_DIR, "edges")
        )

    jobs.append(
        (HOST_RANKS_PATH, HOST_RANKS_OUTPUT_DIR, "ranks")
    )

    total_jobs = len(jobs)

    print("\n" + "=" * 72)
    print("DOWNLOAD PLAN")
    print("=" * 72)
    print(f"Vertex files: {len(vertex_paths):,}")
    print(f"Edge files:   {len(edge_paths):,}")
    print("Host-ranks:   1")
    print(f"Total files:  {total_jobs:,}")
    print(f"Workers:      {MAX_WORKERS}")

    print(f"\nVertices -> {VERTEX_OUTPUT_DIR.resolve()}")
    print(f"Edges    -> {EDGE_OUTPUT_DIR.resolve()}")
    print(f"Host ranks -> {HOST_RANKS_OUTPUT_DIR.resolve()}")

    if not jobs:
        print("\nNothing to download.")
        return

    counters = {
        "vertices": {"downloaded": 0, "skipped": 0, "failed": 0},
        "edges": {"downloaded": 0, "skipped": 0, "failed": 0},
        "ranks": {"downloaded": 0, "skipped": 0, "failed": 0},
    }

    failed = []

    print("\nStarting parallel downloads...\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_job = {
            executor.submit(
                download_file,
                object_path,
                output_dir,
                dataset_type,
            ): (object_path, dataset_type)
            for object_path, output_dir, dataset_type in jobs
        }

        for completed, future in enumerate(
            as_completed(future_to_job),
            start=1,
        ):
            object_path, dataset_type = future_to_job[future]

            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "type": dataset_type,
                    "status": "failed",
                    "url": BASE_URL + object_path,
                    "size": 0,
                    "error": str(exc),
                }

            kind = result["type"]
            status = result["status"]

            counters[kind][status] += 1

            if status == "failed":
                failed.append(result)

            size_mb = result.get("size", 0) / (1024 ** 2)

            print(
                f"[{completed:>3}/{total_jobs}] "
                f"[{kind.upper():8}] "
                f"[{status.upper():10}] "
                f"{os.path.basename(object_path)} "
                f"{size_mb:,.1f} MB"
            )

            if status == "failed":
                print(
                    f"    ERROR: {result.get('error', 'unknown error')}"
                )


    vertex_failures = [
        item for item in failed
        if item["type"] == "vertices"
    ]
    edge_failures = [
        item for item in failed
        if item["type"] == "edges"
    ]
    rank_failures = [
        item for item in failed
        if item["type"] == "ranks"
    ]

    if vertex_failures:
        path = VERTEX_OUTPUT_DIR / "failed_vertex_downloads.txt"
        with open(path, "w", encoding="utf-8") as f:
            for item in vertex_failures:
                f.write(item["url"] + "\n")

    if edge_failures:
        path = EDGE_OUTPUT_DIR / "failed_edge_downloads.txt"
        with open(path, "w", encoding="utf-8") as f:
            for item in edge_failures:
                f.write(item["url"] + "\n")

    if rank_failures:
        path = HOST_RANKS_OUTPUT_DIR / "failed_rank_downloads.txt"
        with open(path, "w", encoding="utf-8") as f:
            for item in rank_failures:
                f.write(item["url"] + "\n")

    print("\n" + "=" * 72)
    print("DOWNLOAD SUMMARY")
    print("=" * 72)

    for kind in ("vertices", "edges", "ranks"):
        stats = counters[kind]

        print(f"\n{kind.upper()}")
        print(f"  Downloaded: {stats['downloaded']:,}")
        print(f"  Skipped:    {stats['skipped']:,}")
        print(f"  Failed:     {stats['failed']:,}")

    vertex_files = sorted(VERTEX_OUTPUT_DIR.glob("*.gz"))
    edge_files = sorted(EDGE_OUTPUT_DIR.glob("*.gz"))

    if vertex_files:
        print("\n" + "-" * 72)
        print("VERTEX SAMPLE")
        preview_gzip_file(vertex_files[0])

    if edge_files:
        print("\n" + "-" * 72)
        print("EDGE SAMPLE")
        preview_gzip_file(edge_files[0])

    print("\nFinished.")


if __name__ == "__main__":
    main()
