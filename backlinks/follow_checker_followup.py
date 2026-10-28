"""
STANDALONE FOLLOW-UP rel CHECKER -- href / rel only, no DOM, no HTMLParser.

Pipeline position:
    graph_filter.py  ->  backlink_index/backlink_graph_filtered.csv
                         (or a legacy name: backlink_edges_filtered.csv, ...)
                     ->  THIS SCRIPT

It does NOT depend on follow_checker.py or its SQLite file. It builds its own
disk-backed pair index (backlink_index/followup_work.sqlite3) straight from the
filtered edge CSV, and reuses that index on later runs as long as the CSV has
not changed. Crawl results always start fresh.

How it stays light:
  * Page bodies stay raw bytes (never decoded as a whole).
  * Small target sets get a substring pre-check: if no target hostname appears
    in the bytes, the page is done without scanning anchors.
  * One regex finds `<a ...>` opening tags; a second pulls only href and rel.
  * Each source homepage is fetched and scanned ONCE for all its targets.
  * Output is one row per (source, target) pair with link counts by rel type.
  * Every pair with a verified link (follow / nofollow / sponsored / ugc) is
    written with its Common Crawl ranks to followup_edges_classified.csv, which
    visual_graph_filter.py reads. followup_edges_dofollow.csv is the follow subset.

Re-export the edge CSVs from an existing followup_status.csv without crawling:
    python follow_checker_followup.py --export-only

Trade-off of skipping the DOM: an `<a href=...>` string inside inline
JavaScript or a <template> also counts. HTML comments are stripped.

Run with Run (not Debug) in PyCharm for full crawls.
"""

import os


os.environ.setdefault("AIOHTTP_NO_EXTENSIONS", "1")

import asyncio
import csv
import faulthandler
import ipaddress
import logging
import re
import socket
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

faulthandler.enable(all_threads=True)



logging.getLogger("aiohttp.client").setLevel(logging.ERROR)
logging.getLogger("aiohttp.internal").setLevel(logging.ERROR)






OUTPUT_DIR = Path("backlink_index")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)




FILTERED_EDGES_CANDIDATES = (
    OUTPUT_DIR / "backlink_graph_filtered.csv",
    OUTPUT_DIR / "back_link_graph_filtered.csv",
    OUTPUT_DIR / "backlink_edges_filtered.csv",
    Path("backlink_graph_filtered.csv"),
    Path("backlink_edges_filtered.csv"),
)

WORK_DB = OUTPUT_DIR / "followup_work.sqlite3"            
INDEX_SCHEMA_VERSION = "followup-1"

FOLLOWUP_STATUS_CSV = OUTPUT_DIR / "followup_status.csv"
FOLLOWUP_PREVIOUS_CSV = OUTPUT_DIR / "followup_status.previous.csv"
FOLLOWUP_SUMMARY_CSV = OUTPUT_DIR / "followup_summary.csv"
FOLLOWUP_DOFOLLOW_CSV = OUTPUT_DIR / "followup_edges_dofollow.csv"
FOLLOWUP_CLASSIFIED_CSV = OUTPUT_DIR / "followup_edges_classified.csv"


LINKED_CLASSES = ("follow", "nofollow", "sponsored", "ugc")










RECHECK_CLASSIFICATIONS = None


MAX_PAIRS = None





MAX_CONCURRENT_REQUESTS = 400

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
TOTAL_REQUEST_TIMEOUT = 15
MAX_RETRIES_PER_SCHEME = 1
RETRY_DELAY = 0.5

TRY_HTTP_FALLBACK = True
HTTP_FALLBACK_FAILURES = {"ssl_failed", "connection_failed", "client_error"}

MAX_HTML_BYTES = 2 * 1024 * 1024  



PREFILTER_MAX_TARGETS = 64

COMMON_CRAWL_REVERSED_HOSTNAMES = True
FORCE_IPV4 = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

INDEX_BATCH_SIZE = 50_000
SOURCE_QUERY_BATCH_SIZE = 2_000
LINKED_DB_BATCH_SIZE = 10_000
PROGRESS_EVERY_SOURCES = 250

EDGE_COLUMNS = (
    "source_domain", "target_domain", "weight", "generation",
    "source_cc_harmonic_rank", "source_cc_harmonic_score",
    "source_cc_pagerank_rank", "source_cc_pagerank_score",
    "target_cc_harmonic_rank", "target_cc_harmonic_score",
    "target_cc_pagerank_rank", "target_cc_pagerank_score",
)







def resolve_input_csv():
    """Select the input edge CSV from command-line arguments or configured candidates."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        return Path(args[0])
    for candidate in FILTERED_EDGES_CANDIDATES:
        if candidate.exists():
            return candidate
    return FILTERED_EDGES_CANDIDATES[0]







def windows_asyncio_exception_handler(loop, context):
    """Suppress known Windows connection-reset shutdown errors and delegate other loop errors."""
    exc = context.get("exception")
    message = str(context.get("message", ""))
    if os.name == "nt":
        if isinstance(exc, ConnectionResetError):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10053, 10054}:
            return
        if (
            "_ProactorBasePipeTransport._call_connection_lost" in message
            and ("10054" in message or "forcibly closed" in message.lower())
        ):
            return
    loop.default_exception_handler(context)


def configure_windows_event_loop():
    """Select the Windows Proactor event-loop policy when available."""
    if os.name != "nt":
        return


    policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_cls is not None:
        asyncio.set_event_loop_policy(policy_cls())







def commoncrawl_to_hostname(domain):
    """com.example.www -> www.example.com"""
    if not domain:
        return ""
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return ""
    try:
        ipaddress.ip_address(domain)
        return domain
    except ValueError:
        pass
    if not COMMON_CRAWL_REVERSED_HOSTNAMES:
        return domain
    parts = [p for p in domain.split(".") if p]
    if len(parts) <= 1:
        return domain
    return ".".join(reversed(parts))


def build_target_lookup(target_domains):
    """live hostname -> [stored Common Crawl target names]"""
    lookup = defaultdict(list)
    for target in target_domains:
        host = commoncrawl_to_hostname(target)
        if host:
            lookup[host].append(target)
    return lookup


def match_targets(link_host, lookup):
    """Match the link host and each parent suffix (sub.example.com -> example.com)."""
    labels = link_host.split(".")
    matches = []
    for i in range(len(labels)):
        hit = lookup.get(".".join(labels[i:]))
        if hit:
            matches.extend(hit)
    return matches


def classify_rel(rel_value):
    """Classify a link rel attribute as follow, nofollow, sponsored, or user-generated."""
    if not rel_value:
        return "follow"
    tokens = {t.lower() for t in rel_value.split()}
    if "nofollow" in tokens:
        return "nofollow"
    if "sponsored" in tokens:
        return "sponsored"
    if "ugc" in tokens:
        return "ugc"
    return "follow"






COMMENT_RE = re.compile(rb"<!--.*?-->", re.S)
ANCHOR_TAG_RE = re.compile(rb"<a\s[^>]*>", re.I)
HREF_REL_RE = re.compile(
    rb"""\s(href|rel)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.I,
)


def _attr_text(raw):
    """Decode an HTML attribute value and unescape its entities."""
    return unescape(raw.decode("utf-8", errors="replace")).strip()


def scan_links(body, target_domains):
    """
    Returns {target: {"follow": n, "nofollow": n, "sponsored": n, "ugc": n,
                      "sample": (href, rel)}}
    for every target with at least one link on the page.
    """
    lookup = build_target_lookup(target_domains)
    if not lookup:
        return {}

    if len(lookup) <= PREFILTER_MAX_TARGETS:
        lowered = body.lower()
        if not any(
            n and n in lowered
            for n in (h.encode("ascii", "ignore") for h in lookup)
        ):
            return {}

    if b"<!--" in body:
        body = COMMENT_RE.sub(b"", body)

    found = {}

    for tag_match in ANCHOR_TAG_RE.finditer(body):
        href = None
        rel = b""
        for attr in HREF_REL_RE.finditer(tag_match.group(0)):
            name = attr.group(1).lower()
            value = next(v for v in attr.groups()[1:] if v is not None)
            if name == b"href" and href is None:
                href = value
            elif name == b"rel":
                rel = value

        if not href:
            continue

        href_text = _attr_text(href)
        try:
            host = urlsplit(href_text).hostname
        except (ValueError, TypeError):
            continue
        if not host:  
            continue

        targets = match_targets(host.rstrip("."), lookup)
        if not targets:
            continue

        rel_text = _attr_text(rel) if rel else ""
        cls = classify_rel(rel_text)

        for target in targets:
            entry = found.get(target)
            if entry is None:
                entry = found[target] = {
                    "follow": 0, "nofollow": 0, "sponsored": 0, "ugc": 0,
                    "sample": (href_text, rel_text),
                }
            entry[cls] += 1
            if cls == "follow" and entry["follow"] == 1:
                entry["sample"] = (href_text, rel_text)  

    return found


def pair_classification(entry):
    """Choose a pair status from its discovered links or mark it absent from the homepage."""
    if entry is None:
        return "not_found_on_homepage"
    if entry["follow"]:
        return "follow"
    return max(("nofollow", "sponsored", "ugc"), key=lambda k: entry[k])







def looks_like_non_html_xml(body):
    """Detect XML feeds and sitemap documents that should not be treated as HTML pages."""
    head = body[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if b"<html" in head:
        return False
    return head.startswith((b"<?xml", b"<rss", b"<feed", b"<urlset", b"<sitemapindex"))


async def read_limited_body(response, max_bytes):
    """Read an asynchronous response body without exceeding the configured byte limit."""
    chunks, total = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        if not chunk:
            break
        remaining = max_bytes - total
        if remaining <= 0:
            break
        chunk = chunk[:remaining]
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def fetch_one_url(session, url):
    """Fetch one URL with retries and return its body, final URL, and failure classification."""
    last_failure, last_url = "client_error", url

    for attempt in range(MAX_RETRIES_PER_SCHEME):
        try:
            async with session.get(url, allow_redirects=True) as response:
                final_url = str(response.url)
                last_url = final_url
                if response.status >= 400:
                    return None, final_url, f"http_{response.status}"

                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type and not any(
                    t in content_type for t in ("html", "text/plain", "xml")
                ):
                    return None, final_url, "non_html"

                body = await read_limited_body(response, MAX_HTML_BYTES)
                if looks_like_non_html_xml(body):
                    return None, final_url, "xml_document"
                return body, final_url, None

        except asyncio.TimeoutError:
            last_failure = "fetch_timeout"
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
            last_failure = "ssl_failed"
        except aiohttp.ClientConnectorDNSError:
            last_failure = "dns_failed"
        except aiohttp.TooManyRedirects:
            last_failure = "redirect_loop"
        except aiohttp.ClientConnectionError:
            last_failure = "connection_failed"
        except aiohttp.ClientError:
            last_failure = "client_error"
        except OSError:
            last_failure = "connection_failed"

        if attempt + 1 < MAX_RETRIES_PER_SCHEME:
            await asyncio.sleep(RETRY_DELAY)

    return None, last_url, last_failure


async def fetch_homepage(session, stored_domain):
    """Fetch a Common Crawl host homepage, optionally retrying over HTTP."""
    live_host = commoncrawl_to_hostname(stored_domain)
    if not live_host:
        return None, "", "invalid_hostname"

    body, final_url, failure = await fetch_one_url(session, f"https://{live_host}/")
    if body is not None:
        return body, final_url, None

    if TRY_HTTP_FALLBACK and failure in HTTP_FALLBACK_FAILURES:
        body2, final2, failure2 = await fetch_one_url(session, f"http://{live_host}/")
        if body2 is not None:
            return body2, final2, None
        return None, final2 or final_url, failure2

    return None, final_url, failure







def open_work_db():
    """Open the SQLite work database and configure its performance and concurrency settings."""
    conn = sqlite3.connect(WORK_DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")  
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def input_signature(csv_path):
    """Build a cache signature from the edge CSV metadata and index settings."""
    stat = csv_path.stat()
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "input_path": str(csv_path.resolve()),
        "input_size": str(stat.st_size),
        "input_mtime_ns": str(stat.st_mtime_ns),
        "max_pairs": str(MAX_PAIRS),
    }


def index_is_current(conn, signature):
    """Check whether the SQLite pair index matches the input signature."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"meta", "pairs", "sources"}.issubset(tables):
        return False
    stored = dict(conn.execute("SELECT key, value FROM meta"))
    return all(stored.get(k) == v for k, v in signature.items())


def build_pair_index(conn, csv_path, signature):
    """Rebuild the SQLite pair/source index from the filtered edge CSV in batches."""
    print(f"\nBuilding pair index from: {csv_path}")
    cols = ", ".join(f"{c} TEXT" for c in EDGE_COLUMNS[2:])
    conn.executescript(
        f"""
        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS pairs;
        DROP TABLE IF EXISTS sources;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE pairs (
            source_domain TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            {cols},
            PRIMARY KEY (source_domain, target_domain)
        ) WITHOUT ROWID;
        CREATE TABLE sources (source_domain TEXT PRIMARY KEY) WITHOUT ROWID;
        """
    )

    insert_sql = (
        f"INSERT OR IGNORE INTO pairs ({', '.join(EDGE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(EDGE_COLUMNS))})"
    )
    batch, sources = [], set()
    rows_read = 0
    started = time.perf_counter()

    def flush():
        """Persist pending pair and source batches to SQLite and clear their buffers."""
        conn.executemany(insert_sql, batch)
        conn.executemany(
            "INSERT OR IGNORE INTO sources(source_domain) VALUES (?)",
            ((s,) for s in sources),
        )
        conn.commit()
        batch.clear()
        sources.clear()

    with open(csv_path, newline="", encoding="utf-8", buffering=1024 * 1024) as f:
        for row in csv.DictReader(f):
            src = (row.get("source_domain") or "").strip()
            dst = (row.get("target_domain") or "").strip()
            if not src or not dst or src == dst:
                continue

            batch.append(
                (src, dst, row.get("weight") or "1",
                 *((row.get(c) or "") for c in EDGE_COLUMNS[3:]))
            )
            sources.add(src)
            rows_read += 1

            if len(batch) >= INDEX_BATCH_SIZE:
                flush()
                rate = rows_read / max(time.perf_counter() - started, 0.001)
                print(f"\rIndexed {rows_read:,} rows ({rate:,.0f} rows/s)", end="", flush=True)

            if MAX_PAIRS is not None and rows_read >= MAX_PAIRS:
                break

    if batch:
        flush()

    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", signature.items())
    conn.commit()
    print(f"\rIndexed {rows_read:,} rows in {time.perf_counter() - started:,.0f}s")


def build_or_reuse_index(conn, csv_path):
    """Reuse a matching SQLite pair index or rebuild it from the input CSV."""
    signature = input_signature(csv_path)
    if index_is_current(conn, signature):
        print("\nReusing pair index (filtered CSV unchanged).")
    else:
        build_pair_index(conn, csv_path, signature)


LINKED_INSERT_SQL = "INSERT OR REPLACE INTO linked VALUES (?, ?, ?, ?, ?, ?, ?)"


def create_linked_table(conn):
    """Pairs where a link was found, with its rel classification and counts."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS dofollow;
        DROP TABLE IF EXISTS linked;
        CREATE TABLE linked (
            source_domain TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            classification TEXT NOT NULL,
            follow_links INTEGER NOT NULL,
            nofollow_links INTEGER NOT NULL,
            sponsored_links INTEGER NOT NULL,
            ugc_links INTEGER NOT NULL,
            PRIMARY KEY (source_domain, target_domain)
        ) WITHOUT ROWID;
        """
    )


def _int(value):
    """Convert a value to an integer, treating empty or invalid numeric text as zero."""
    try:
        return int(value or 0)
    except ValueError:
        return 0


def linked_tuple(row):
    """followup_status.csv row -> linked table tuple (or None if no link)."""
    cls = row.get("classification")
    src, dst = row.get("source_domain"), row.get("target_domain")
    if cls not in LINKED_CLASSES or not src or not dst:
        return None
    return (src, dst, cls, _int(row.get("follow_links")), _int(row.get("nofollow_links")),
            _int(row.get("sponsored_links")), _int(row.get("ugc_links")))


def load_linked_from_status(conn, status_csv):
    """Fill the linked table from an existing status CSV. Returns classification counts."""
    create_linked_table(conn)
    counts, batch = Counter(), []
    with open(status_csv, newline="", encoding="utf-8", buffering=1024 * 1024) as f:
        for row in csv.DictReader(f):
            counts[row.get("classification")] += 1
            item = linked_tuple(row)
            if item:
                batch.append(item)
            if len(batch) >= INDEX_BATCH_SIZE:
                conn.executemany(LINKED_INSERT_SQL, batch)
                batch.clear()
    if batch:
        conn.executemany(LINKED_INSERT_SQL, batch)
    conn.commit()
    return counts


def prepare_crawl_tables(conn):
    """Reset results; return (pair_table, source_table) to crawl."""
    create_linked_table(conn)
    conn.executescript(
        """
        DROP TABLE IF EXISTS recheck_pairs;
        DROP TABLE IF EXISTS recheck_sources;
        """
    )

    if RECHECK_CLASSIFICATIONS is None:
        conn.commit()
        return "pairs", "sources"

    if not FOLLOWUP_PREVIOUS_CSV.exists():
        raise RuntimeError(
            "RECHECK_CLASSIFICATIONS is set but there is no previous "
            f"{FOLLOWUP_STATUS_CSV.name} to select from. Run once with None first."
        )

    print(f"\nSelecting pairs from {FOLLOWUP_PREVIOUS_CSV} with classification in "
          f"{sorted(RECHECK_CLASSIFICATIONS)}")
    conn.executescript(
        """
        CREATE TABLE recheck_pairs (
            source_domain TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            PRIMARY KEY (source_domain, target_domain)
        ) WITHOUT ROWID;
        CREATE TABLE recheck_sources (source_domain TEXT PRIMARY KEY) WITHOUT ROWID;
        """
    )



    batch, carried = [], []
    with open(FOLLOWUP_PREVIOUS_CSV, newline="", encoding="utf-8",
              buffering=1024 * 1024) as f:
        for row in csv.DictReader(f):
            if row.get("classification") in RECHECK_CLASSIFICATIONS:
                src, dst = row.get("source_domain"), row.get("target_domain")
                if src and dst:
                    batch.append((src, dst))
            else:
                item = linked_tuple(row)
                if item:
                    carried.append(item)
            if len(batch) >= INDEX_BATCH_SIZE:
                conn.executemany("INSERT OR IGNORE INTO recheck_pairs VALUES (?, ?)", batch)
                batch.clear()
            if len(carried) >= INDEX_BATCH_SIZE:
                conn.executemany(LINKED_INSERT_SQL, carried)
                carried.clear()
    if batch:
        conn.executemany("INSERT OR IGNORE INTO recheck_pairs VALUES (?, ?)", batch)
    if carried:
        conn.executemany(LINKED_INSERT_SQL, carried)
    conn.execute(
        "INSERT OR IGNORE INTO recheck_sources SELECT DISTINCT source_domain FROM recheck_pairs"
    )
    conn.commit()
    return "recheck_pairs", "recheck_sources"






STATUS_FIELDS = [
    "source_domain", "target_domain", "classification",
    "follow_links", "nofollow_links", "sponsored_links", "ugc_links",
    "sample_link_url", "sample_rel", "checked_url",
]


def build_rows(src_domain, target_domains, body, final_url, failure):
    """One row per pair. Returns (rows, linked) -- linked = linked-table tuples."""
    final_url = final_url or ""
    if body is None:
        cls = failure or "fetch_failed"
        return [[src_domain, t, cls, 0, 0, 0, 0, "", "", final_url] for t in target_domains], []

    found = scan_links(body, target_domains)
    rows, linked = [], []
    for t in target_domains:
        entry = found.get(t)
        cls = pair_classification(entry)
        if entry:
            href, rel = entry["sample"]
            rows.append([src_domain, t, cls, entry["follow"], entry["nofollow"],
                         entry["sponsored"], entry["ugc"], href, rel, final_url])
        else:
            rows.append([src_domain, t, cls, 0, 0, 0, 0, "", "", final_url])
        if entry and cls in LINKED_CLASSES:
            linked.append((src_domain, t, cls, entry["follow"], entry["nofollow"],
                           entry["sponsored"], entry["ugc"]))
    return rows, linked


def write_summary(counts):
    """Write classification counts to the follow-up summary CSV."""
    with open(FOLLOWUP_SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["classification", "count"])
        w.writerows(counts.most_common())


LINK_COLUMNS = ("link_type", "follow_links", "nofollow_links", "sponsored_links", "ugc_links")


def _export_linked(conn, path, columns, where=""):
    """Export verified linked pairs from SQLite using the requested columns and filter."""
    select = ", ".join(f"p.{c}" for c in EDGE_COLUMNS)
    if columns is not EDGE_COLUMNS:
        select += (", l.classification, l.follow_links, l.nofollow_links, "
                   "l.sponsored_links, l.ugc_links")
    cursor = conn.execute(
        f"SELECT {select} "
        "FROM pairs AS p JOIN linked AS l "
        "ON l.source_domain = p.source_domain AND l.target_domain = p.target_domain "
        f"{where} ORDER BY p.source_domain, p.target_domain"
    )
    kept = 0
    with open(path, "w", newline="", encoding="utf-8", buffering=1024 * 1024) as f:
        w = csv.writer(f)
        w.writerow(columns)
        while True:
            rows = cursor.fetchmany(20_000)
            if not rows:
                break
            w.writerows(rows)
            kept += len(rows)
    return kept


def write_dofollow_edges(conn):
    """Export verified follow-link pairs using the standard edge columns."""
    return _export_linked(conn, FOLLOWUP_DOFOLLOW_CSV, EDGE_COLUMNS,
                          "WHERE l.classification = 'follow'")


def write_classified_edges(conn):
    """Every verified link (follow/nofollow/sponsored/ugc) with ranks + link_type."""
    return _export_linked(conn, FOLLOWUP_CLASSIFIED_CSV, (*EDGE_COLUMNS, *LINK_COLUMNS))







async def crawl(conn, pair_table, source_table, total_pairs, total_sources):
    """Crawl selected source homepages, store pair statuses, and export progress counts."""
    loop = asyncio.get_running_loop()
    if os.name == "nt":
        loop.set_exception_handler(windows_asyncio_exception_handler)

    timeout = aiohttp.ClientTimeout(
        total=TOTAL_REQUEST_TIMEOUT,
        connect=CONNECT_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=READ_TIMEOUT,
    )
    connector_kwargs = {
        "limit": MAX_CONCURRENT_REQUESTS,
        "limit_per_host": 2,
        "ttl_dns_cache": 300,
        "force_close": True,
        "enable_cleanup_closed": False,
    }
    if FORCE_IPV4:
        connector_kwargs["family"] = socket.AF_INET

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    work_queue = asyncio.Queue(maxsize=MAX_CONCURRENT_REQUESTS * 2)
    result_queue = asyncio.Queue(maxsize=MAX_CONCURRENT_REQUESTS * 2)
    counts = Counter()
    state = {"started": time.perf_counter(), "sources": 0, "pairs": 0}

    targets_sql = f"SELECT target_domain FROM {pair_table} WHERE source_domain = ?"
    sources_sql = (
        f"SELECT source_domain FROM {source_table} WHERE source_domain > ? "
        "ORDER BY source_domain LIMIT ?"
    )

    status_file = open(FOLLOWUP_STATUS_CSV, "w", newline="", encoding="utf-8",
                       buffering=1024 * 1024)
    status_writer = csv.writer(status_file)
    status_writer.writerow(STATUS_FIELDS)

    async def producer():
        """Read source hosts from SQLite in batches and feed them to the crawl workers."""
        last = ""
        while True:
            batch = conn.execute(sources_sql, (last, SOURCE_QUERY_BATCH_SIZE)).fetchall()
            if not batch:
                break
            for (src,) in batch:
                await work_queue.put(src)
            last = batch[-1][0]
        for _ in range(MAX_CONCURRENT_REQUESTS):
            await work_queue.put(None)

    async def worker(session):



        """Fetch a source homepage, build its target-pair results, and send them to the writer."""
        while True:
            src = await work_queue.get()
            if src is None:
                return
            try:
                body, final_url, failure = await fetch_homepage(session, src)
                targets = [r[0] for r in conn.execute(targets_sql, (src,))]
                rows, linked = build_rows(src, targets, body, final_url, failure)
            except Exception as exc:
                if session.closed:


                    raise
                rows = [[src, "", "error", 0, 0, 0, 0, "", "", type(exc).__name__]]
                linked = []
            await result_queue.put((src, rows, linked))

    async def writer():
        """Write crawl results and batch verified links into SQLite while reporting progress."""
        pending = []
        while True:
            item = await result_queue.get()
            if item is None:
                break
            src, rows, linked = item
            status_writer.writerows(rows)
            counts.update(r[2] for r in rows)
            pending.extend(linked)
            if len(pending) >= LINKED_DB_BATCH_SIZE:
                conn.executemany(LINKED_INSERT_SQL, pending)
                conn.commit()
                pending.clear()

            state["sources"] += 1
            state["pairs"] += len(rows)
            done = state["sources"]
            if done % PROGRESS_EVERY_SOURCES == 0 or done == total_sources:
                elapsed = max(time.perf_counter() - state["started"], 0.001)
                rate = done / elapsed
                eta = (total_sources - done) / rate if rate else 0
                print(
                    f"\rSources [{done:,}/{total_sources:,}]  "
                    f"Pairs [{state['pairs']:,}/{total_pairs:,}]  "
                    f"{rate:,.1f} src/s  ETA ~{eta / 3600:,.2f}h",
                    end="", flush=True,
                )

        if pending:
            conn.executemany(LINKED_INSERT_SQL, pending)
            conn.commit()

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(**connector_kwargs),
            headers=headers,
            auto_decompress=True,
            trust_env=True,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            writer_task = asyncio.create_task(writer())
            feeders = [asyncio.create_task(producer())]
            feeders += [asyncio.create_task(worker(session))
                        for _ in range(MAX_CONCURRENT_REQUESTS)]
            feed = asyncio.gather(*feeders)
            try:
                done, _ = await asyncio.wait(
                    {feed, writer_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if writer_task in done:
                    writer_task.result()  
                    raise RuntimeError("writer stopped before the crawl finished")
                feed.result()  
                await result_queue.put(None)
                await writer_task
            finally:


                for t in (*feeders, writer_task):
                    t.cancel()
                await asyncio.gather(feed, *feeders, writer_task, return_exceptions=True)
    finally:
        status_file.close()

    print()
    return counts, state







def export_only(input_csv):
    """Rebuild the edge CSVs from an existing followup_status.csv (no crawling)."""
    if not FOLLOWUP_STATUS_CSV.exists():
        print(f"ERROR: {FOLLOWUP_STATUS_CSV} not found -- run a crawl first.")
        return
    print(f"Export only: reading {FOLLOWUP_STATUS_CSV}")
    conn = open_work_db()
    try:
        build_or_reuse_index(conn, input_csv)
        counts = load_linked_from_status(conn, FOLLOWUP_STATUS_CSV)
        write_summary(counts)
        kept = write_dofollow_edges(conn)
        classified = write_classified_edges(conn)
    finally:
        conn.close()
    print(f"Dofollow pairs: {kept:,}  ->  {FOLLOWUP_DOFOLLOW_CSV}")
    print(f"Linked pairs:   {classified:,}  ->  {FOLLOWUP_CLASSIFIED_CSV}")


def main():
    """Resolve inputs, prepare the work database, run or export the follow-up crawl, and report results."""
    if sys.gettrace() is not None or "debugpy" in sys.modules or "pydevd" in sys.modules:
        print(
            "WARNING: running under a debugger. Every caught DNS/connection error "
            "is reported to it, which is very slow and floods the console. "
            "Use Run (Shift+F10), not Debug, for full crawls.\n"
        )

    input_csv = resolve_input_csv()
    if not input_csv.exists():
        print(f"ERROR: filtered edge list not found: {input_csv}")
        print("Run graph_filter.py first, or pass the CSV path as an argument.")
        return

    if "--export-only" in sys.argv:
        export_only(input_csv)
        return

    print("=" * 78)
    print("FOLLOW-UP rel CHECK (standalone) -- href/rel regex scan, no DOM")
    print("=" * 78)
    print(f"Input:               {input_csv}")
    print(f"Work DB:             {WORK_DB}")
    print(f"Pair selection:      "
          f"{'ALL pairs' if RECHECK_CLASSIFICATIONS is None else sorted(RECHECK_CLASSIFICATIONS)}")
    print(f"Concurrent requests: {MAX_CONCURRENT_REQUESTS}")
    print(f"Max bytes/page:      {MAX_HTML_BYTES / 1024 / 1024:.1f} MiB")


    try:
        if RECHECK_CLASSIFICATIONS is not None and FOLLOWUP_STATUS_CSV.exists():
            os.replace(FOLLOWUP_STATUS_CSV, FOLLOWUP_PREVIOUS_CSV)
        for path in (FOLLOWUP_STATUS_CSV, FOLLOWUP_SUMMARY_CSV, FOLLOWUP_DOFOLLOW_CSV,
                     FOLLOWUP_CLASSIFIED_CSV):
            path.unlink(missing_ok=True)
    except PermissionError as exc:
        print(f"\nERROR: {exc.filename} is open in another program. Close it and re-run.")
        return

    conn = open_work_db()
    try:
        build_or_reuse_index(conn, input_csv)
        pair_table, source_table = prepare_crawl_tables(conn)

        total_pairs = conn.execute(f"SELECT COUNT(*) FROM {pair_table}").fetchone()[0]
        total_sources = conn.execute(f"SELECT COUNT(*) FROM {source_table}").fetchone()[0]
        print(f"\nPairs to check:      {total_pairs:,}")
        print(f"Source homepages:    {total_sources:,}\n")
        if not total_pairs:
            print("Nothing to check.")
            return

        configure_windows_event_loop()
        counts, state = asyncio.run(
            crawl(conn, pair_table, source_table, total_pairs, total_sources)
        )

        write_summary(counts)
        kept = write_dofollow_edges(conn)
        classified = write_classified_edges(conn)
        elapsed = max(time.perf_counter() - state["started"], 0.001)

        print("=" * 78)
        print(f"Sources checked:  {state['sources']:,}")
        print(f"Pairs checked:    {state['pairs']:,}")
        print(f"Elapsed:          {elapsed / 3600:,.2f} h  "
              f"({state['sources'] / elapsed:,.1f} src/s)")
        print("\nClassifications:")
        for cls, n in counts.most_common():
            print(f"  {cls:26} {n:,}")
        print(f"\nDofollow pairs:   {kept:,} / {total_pairs:,}")
        print(f"Linked pairs:     {classified:,} (follow + nofollow + sponsored + ugc)")
        print(f"\nStatus:     {FOLLOWUP_STATUS_CSV}")
        print(f"Summary:    {FOLLOWUP_SUMMARY_CSV}")
        print(f"Dofollow:   {FOLLOWUP_DOFOLLOW_CSV}")
        print(f"Classified: {FOLLOWUP_CLASSIFIED_CSV}")

    except RuntimeError as exc:
        import traceback
        print(f"\nERROR: {exc}")
        traceback.print_exc()
    finally:
        conn.close()


if __name__ == "__main__":
    main()