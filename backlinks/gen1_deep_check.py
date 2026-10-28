"""
GEN-1 DEEP LINK CHECK -- verifies the direct (generation 1) backlinks to the
client domains much more thoroughly than the homepage-only follow-up crawl.

Why: Common Crawl's host graph records a link if ANY page on the source host
links to the target, but follow_checker_followup.py only fetches the source
homepage. Most generation-1 links (blog posts, directories, partner pages) live
on inner pages, so they show up as "not_found_on_homepage".

What this script does differently (generation 1 ONLY):
  1. Deep crawl: after the homepage, fetches up to MAX_PAGES_PER_SOURCE same-site
     pages per source, taken from its sitemaps and internal links, ranked so the
     likeliest pages come first (pages mentioning the client brand, partner /
     resources / links / sponsors pages, ...). Stops as soon as every target of
     that source has been found.
  2. Looser host matching:
       exact      www.x.com and x.com are treated as the same host
       same_site  any host on the same registrable domain
                  (example-bistro.com/cordova counts for cordova.example-bistro.com)
  3. Honest labels for misses:
       mentioned_not_linked   the client domain appears in page text but not as a link
                              (link-reclamation opportunity)
       js_rendered_homepage   homepage has almost no <a> tags (links are built by
                              JavaScript, so this crawler can't see them)
       not_found_on_site      nothing found in the pages that were checked

Input:  backlink_index/backlink_edges.csv  (generation-1 rows are at the top)
Output: backlink_index/gen1_deep_status.csv   one row per (source, target)
        backlink_index/gen1_deep_summary.csv  counts per client domain

Run:    python gen1_deep_check.py
"""

import asyncio
import csv
import gzip
import heapq
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

import follow_checker_followup as fc





EDGES_CSV = fc.OUTPUT_DIR / "backlink_edges.csv"
GENERATION = "1"

STATUS_CSV = fc.OUTPUT_DIR / "gen1_deep_status.csv"
SUMMARY_CSV = fc.OUTPUT_DIR / "gen1_deep_summary.csv"

MAX_PAGES_PER_SOURCE = 75        
CONCURRENT_SOURCES = 25          
PAGE_DELAY = 0.3                 
MAX_SITEMAPS_PER_SOURCE = 8      
MAX_SITEMAP_URLS = 5000          
MAX_PAGE_BYTES = 3 * 1024 * 1024
JS_RENDERED_MAX_ANCHORS = 5      


LINK_PAGE_HINTS = {
    "partner": 40, "partners": 40, "sponsor": 40, "sponsors": 40,
    "resource": 35, "resources": 35, "links": 35, "friends": 30,
    "vendor": 30, "vendors": 30, "supplier": 30, "suppliers": 30,
    "directory": 30, "members": 25, "member": 25, "clients": 25,
    "client": 25, "portfolio": 25, "testimonials": 20, "credits": 20,
    "recommended": 25, "blogroll": 35, "useful": 20, "community": 15,
    "about": 10, "contact": 5, "blog": 8, "news": 8, "press": 10,
    "events": 10, "listing": 15, "listings": 15, "local": 10,
}

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".pdf", ".zip", ".gz", ".rar", ".7z", ".mp3", ".mp4", ".mov", ".avi",
    ".webm", ".css", ".js", ".json", ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dmg",
)
SKIP_PATH_PARTS = (
    "/wp-admin", "/wp-login", "/wp-json", "/cart", "/checkout", "/login",
    "/signin", "/sign-in", "/register", "/account", "/feed", "/xmlrpc",
    "/cdn-cgi/", "/tag/", "/author/", "/search", "?s=", "/print/",
)

LINKED = ("follow", "nofollow", "sponsored", "ugc")

STATUS_FIELDS = [
    "source_domain", "target_domain", "classification", "match_type",
    "follow_links", "nofollow_links", "sponsored_links", "ugc_links",
    "found_on_url", "sample_link_url", "sample_rel", "mention_url",
    "pages_checked", "homepage_url",
]





try:
    import tldextract

    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
except Exception:  
    _EXTRACT = None

_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go"}


def strip_www(host):
    """Lowercase a hostname, remove its trailing dot, and strip a leading www label."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def site_of(host):
    """Registrable domain: cordova.example-bistro.com -> example-bistro.com"""
    host = strip_www(host)
    if not host:
        return ""
    if _EXTRACT is not None:
        ext = _EXTRACT(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return host
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class TargetMatcher:
    def __init__(self, targets):
        """Build exact-host, registrable-site, and text-search indexes for the targets."""
        self.exact = defaultdict(list)   
        self.site = defaultdict(list)    
        self.needles = {}                
        for t in targets:
            host = strip_www(fc.commoncrawl_to_hostname(t))
            if not host:
                continue
            self.exact[host].append(t)
            self.site[site_of(host)].append(t)
            self.needles[t] = {host.encode(), site_of(host).encode()}

    def match(self, link_host):
        """Return target matches for a link host, distinguishing exact and same-site matches."""
        host = strip_www(link_host)
        hits = {t: "exact" for t in self.exact.get(host, ())}
        for t in self.site.get(site_of(host), ()):
            hits.setdefault(t, "same_site")
        return hits

    def brand_tokens(self):
        """Words that identify the client in a URL path (example-bistro -> example, bistro)."""
        tokens = set()
        for site in self.site:
            name = site.split(".")[0]
            tokens.add(name)
            tokens.update(p for p in re.split(r"[-_]", name) if len(p) >= 4)
        return tokens







def scan_page(body, page_url, matcher, source_site):
    """
    Returns (found, internal_links, anchor_count)
      found: {target: {"match": exact|same_site, follow.., "sample": (href, rel)}}
    """
    if b"<!--" in body:
        body = fc.COMMENT_RE.sub(b"", body)

    found, internal, anchors = {}, [], 0
    for tag in fc.ANCHOR_TAG_RE.finditer(body):
        anchors += 1
        href, rel = None, b""
        for attr in fc.HREF_REL_RE.finditer(tag.group(0)):
            name = attr.group(1).lower()
            value = next(v for v in attr.groups()[1:] if v is not None)
            if name == b"href" and href is None:
                href = value
            elif name == b"rel":
                rel = value
        if not href:
            continue

        href_text = fc._attr_text(href)
        if href_text.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        try:
            absolute = urljoin(page_url, href_text)
            host = urlsplit(absolute).hostname
        except (ValueError, TypeError):
            continue
        if not host:
            continue

        hits = matcher.match(host)
        if hits:
            rel_text = fc._attr_text(rel) if rel else ""
            cls = fc.classify_rel(rel_text)
            for target, how in hits.items():
                e = found.get(target)
                if e is None or (e["match"] == "same_site" and how == "exact"):
                    e = found[target] = {"match": how, "follow": 0, "nofollow": 0,
                                         "sponsored": 0, "ugc": 0,
                                         "sample": (absolute, rel_text)}
                elif e["match"] != how:
                    continue  
                e[cls] += 1
                if cls == "follow" and e["follow"] == 1:
                    e["sample"] = (absolute, rel_text)
        elif site_of(host) == source_site:
            internal.append(absolute)

    return found, internal, anchors


def find_mentions(body, matcher, targets):
    """Find targets whose host or site strings appear in the page body."""
    lowered = body.lower()
    return {t for t in targets if any(n and n in lowered for n in matcher.needles.get(t, ()))}


def better(new, old):
    """Is a found-entry on a new page better than the one we already have?"""
    if old is None:
        return True
    rank = lambda e: (e["match"] == "exact", e["follow"] > 0)
    return rank(new) > rank(old)







async def fetch_raw(session, url, max_bytes=MAX_PAGE_BYTES):
    """Returns (body or None, final_url, content_type, failure)."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            final = str(resp.url)
            if resp.status >= 400:
                return None, final, "", f"http_{resp.status}"
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = await fc.read_limited_body(resp, max_bytes)
            return body, final, ctype, None
    except asyncio.TimeoutError:
        return None, url, "", "fetch_timeout"
    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
        return None, url, "", "ssl_failed"
    except aiohttp.ClientConnectorDNSError:
        return None, url, "", "dns_failed"
    except aiohttp.TooManyRedirects:
        return None, url, "", "redirect_loop"
    except (aiohttp.ClientError, OSError):
        return None, url, "", "connection_failed"


def is_html(ctype, body):
    """Reject non-HTML content types and XML-like documents."""
    if ctype and "html" not in ctype and "text/plain" not in ctype:
        return False
    return not fc.looks_like_non_html_xml(body)


LOC_RE = re.compile(rb"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.I)
ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)


async def collect_sitemap_urls(session, base_url, source_site):
    """Fetch sitemap declarations and collect same-site page URLs within configured limits."""
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    queue = []
    body, _, _, _ = await fetch_raw(session, root + "/robots.txt", 256 * 1024)
    if body:
        queue += ROBOTS_SITEMAP_RE.findall(body.decode("utf-8", "replace"))
    if not queue:
        queue = [root + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]

    pages, seen, fetched = [], set(), 0
    while queue and fetched < MAX_SITEMAPS_PER_SOURCE and len(pages) < MAX_SITEMAP_URLS:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        body, _, _, failure = await fetch_raw(session, sm, 20 * 1024 * 1024)
        fetched += 1
        if not body:
            continue
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                continue
        is_index = b"<sitemapindex" in body[:2000].lower()
        for m in LOC_RE.finditer(body):
            loc = m.group(1).decode("utf-8", "replace").strip()
            if is_index or loc.lower().split("?")[0].endswith((".xml", ".xml.gz")):
                queue.append(loc)
            elif site_of(urlsplit(loc).hostname or "") == source_site:
                pages.append(loc)
                if len(pages) >= MAX_SITEMAP_URLS:
                    break
    return pages







def normalize_url(url):
    """Validate and canonicalize a crawl URL while excluding unwanted paths and file types."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    path = p.path or "/"
    low = (path + ("?" + p.query if p.query else "")).lower()
    if low.endswith(SKIP_EXTENSIONS) or any(s in low for s in SKIP_PATH_PARTS):
        return None
    return urlunsplit((p.scheme, p.netloc.lower(), path, p.query, ""))


def url_score(url, brand_tokens, from_sitemap):
    """Prioritize URLs likely to contain client links and prefer shallower pages."""
    path = urlsplit(url).path.lower()
    words = set(re.split(r"[^a-z0-9]+", path))
    score = sum(v for k, v in LINK_PAGE_HINTS.items() if k in words)
    if any(tok in path for tok in brand_tokens):
        score += 100  
    score -= path.count("/") * 2  
    if not from_sitemap:
        score += 3  
    if re.search(r"/page/\d+|[?&]page=\d+", url.lower()):
        score -= 15
    return score


async def check_source(session, src, targets):
    """Inspect a source homepage and selected internal pages for links or mentions of targets."""
    matcher = TargetMatcher(targets)
    best, mention_url = {}, {}

    body, home_url, failure = await fc.fetch_homepage(session, src)
    if body is None:
        return [_row(src, t, failure or "fetch_failed", None, "", 0, home_url) for t in targets]

    source_site = site_of(urlsplit(home_url).hostname or fc.commoncrawl_to_hostname(src))
    brand = matcher.brand_tokens()
    pages_checked, visited = 0, set()
    frontier, counter = [], 0

    def push(url, from_sitemap):
        """Normalize and enqueue an unvisited page according to its crawl priority."""
        nonlocal counter
        u = normalize_url(url)
        if u and u not in visited:
            counter += 1
            heapq.heappush(frontier, (-url_score(u, brand, from_sitemap), counter, u))

    def absorb(page_body, page_url):
        """Record target links and mentions found on a page and enqueue its internal links."""
        nonlocal pages_checked
        pages_checked += 1
        found, internal, anchors = scan_page(page_body, page_url, matcher, source_site)
        for t, e in found.items():
            if better(e, best.get(t)):
                e["url"] = page_url
                best[t] = e
        for t in find_mentions(page_body, matcher, targets):
            mention_url.setdefault(t, page_url)
        for link in internal:
            push(link, False)
        return anchors

    visited.add(normalize_url(home_url) or home_url)
    home_anchors = absorb(body, home_url)
    js_rendered = home_anchors < JS_RENDERED_MAX_ANCHORS

    def done():
        """Return whether every target has an exact link match."""
        return all(t in best and best[t]["match"] == "exact" for t in targets)

    if not done():
        for u in await collect_sitemap_urls(session, home_url, source_site):
            push(u, True)

    while frontier and pages_checked < MAX_PAGES_PER_SOURCE and not done():
        _, _, url = heapq.heappop(frontier)
        if url in visited:
            continue
        visited.add(url)
        await asyncio.sleep(PAGE_DELAY)
        page, final, ctype, failure = await fetch_raw(session, url)
        if page is None or not is_html(ctype, page):
            continue
        visited.add(normalize_url(final) or final)
        absorb(page, final)

    rows = []
    for t in targets:
        e = best.get(t)
        if e:
            cls = "follow" if e["follow"] else max(("nofollow", "sponsored", "ugc"), key=e.get)
        elif t in mention_url:
            cls = "mentioned_not_linked"
        elif js_rendered:
            cls = "js_rendered_homepage"
        else:
            cls = "not_found_on_site"
        rows.append(_row(src, t, cls, e, mention_url.get(t, ""), pages_checked, home_url))
    return rows


def _row(src, target, cls, e, mention, pages, home_url):
    """Format one source-target check result as an output CSV row."""
    if e is None:
        return [src, target, cls, "", 0, 0, 0, 0, "", "", "", mention, pages, home_url]
    href, rel = e["sample"]
    return [src, target, cls, e["match"], e["follow"], e["nofollow"], e["sponsored"],
            e["ugc"], e["url"], href, rel, mention, pages, home_url]







def load_pairs(path):
    """{source: [targets]} for generation-1 rows (they sit at the top of the file)."""
    pairs = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gen = (row.get("generation") or "").strip()
            if gen != GENERATION:
                if pairs:
                    break  
                continue
            src = (row.get("source_domain") or "").strip()
            dst = (row.get("target_domain") or "").strip()
            if src and dst and src != dst and dst not in pairs[src]:
                pairs[src].append(dst)
    return pairs


async def run(pairs):
    """Crawl all source-target groups concurrently and return classification counts and rows."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(fc.windows_asyncio_exception_handler)

    timeout = aiohttp.ClientTimeout(total=fc.TOTAL_REQUEST_TIMEOUT + 10,
                                    connect=fc.CONNECT_TIMEOUT + 5,
                                    sock_connect=fc.CONNECT_TIMEOUT + 5,
                                    sock_read=fc.READ_TIMEOUT + 10)
    connector = aiohttp.TCPConnector(
        limit=CONCURRENT_SOURCES * 2, limit_per_host=2, ttl_dns_cache=300,
        family=socket.AF_INET if fc.FORCE_IPV4 else 0,
    )
    headers = {
        "User-Agent": fc.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    sem = asyncio.Semaphore(CONCURRENT_SOURCES)
    counts, started, finished = Counter(), time.perf_counter(), 0

    with open(STATUS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(STATUS_FIELDS)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                         headers=headers, trust_env=True,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:

            async def one(src, targets):
                """Check one source under the concurrency limit and persist its result rows."""
                nonlocal finished
                async with sem:
                    try:
                        rows = await check_source(session, src, targets)
                    except Exception as exc:  
                        rows = [_row(src, t, f"error:{type(exc).__name__}", None, "", 0, "")
                                for t in targets]
                w.writerows(rows)
                f.flush()
                counts.update(r[2] for r in rows)
                finished += 1
                print(f"\rSources [{finished}/{len(pairs)}]  "
                      f"{time.perf_counter() - started:,.0f}s", end="", flush=True)
                return rows

            results = await asyncio.gather(*(one(s, t) for s, t in pairs.items()))
    print()
    return counts, [r for rows in results for r in rows]


def write_summary(all_rows):
    """Write per-client classification totals and return their counters."""
    per_client = defaultdict(Counter)
    for r in all_rows:
        client = site_of(fc.commoncrawl_to_hostname(r[1]))
        per_client[client][r[2]] += 1
    classes = sorted({c for cnt in per_client.values() for c in cnt})
    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["client_domain", "pairs", "linked", *classes])
        for client, cnt in sorted(per_client.items()):
            linked = sum(cnt[c] for c in LINKED)
            w.writerow([client, sum(cnt.values()), linked, *(cnt[c] for c in classes)])
    return per_client


def main():
    """Load generation-one pairs, exclude self-links, run the deep check, and report results."""
    edges = Path(sys.argv[1]) if len(sys.argv) > 1 else EDGES_CSV
    if not edges.exists():
        print(f"ERROR: {edges} not found.")
        return
    pairs = load_pairs(edges)



    self_links = 0
    for src in list(pairs):
        src_site = site_of(fc.commoncrawl_to_hostname(src))
        kept = [t for t in pairs[src] if site_of(fc.commoncrawl_to_hostname(t)) != src_site]
        self_links += len(pairs[src]) - len(kept)
        if kept:
            pairs[src] = kept
        else:
            del pairs[src]

    total = sum(len(t) for t in pairs.values())
    print("=" * 78)
    print("GEN-1 DEEP LINK CHECK")
    print("=" * 78)
    print(f"Input:            {edges}")
    print(f"Generation {GENERATION} pairs: {total:,} from {len(pairs):,} source hosts")
    print(f"Skipped:          {self_links:,} self-links (client host -> its own other host)")
    print(f"Pages/source:     up to {MAX_PAGES_PER_SOURCE}")
    if _EXTRACT is None:
        print("Note: tldextract not installed -- using a simple domain heuristic.")
    if not pairs:
        print("Nothing to check.")
        return

    fc.configure_windows_event_loop()
    counts, all_rows = asyncio.run(run(pairs))
    per_client = write_summary(all_rows)

    print("\nClassifications:")
    for cls, n in counts.most_common():
        print(f"  {cls:24} {n:,}")
    print("\nPer client (linked / pairs):")
    for client, cnt in sorted(per_client.items(), key=lambda kv: -sum(kv[1].values())):
        linked = sum(cnt[c] for c in LINKED)
        print(f"  {client:36} {linked:>4} / {sum(cnt.values()):<4}"
              f"  follow {cnt['follow']}, mentions {cnt['mentioned_not_linked']}")
    print(f"\nStatus:  {STATUS_CSV}\nSummary: {SUMMARY_CSV}")


if __name__ == "__main__":
    main()
