import csv
import gzip
import hashlib
import html as html_lib
import json
import math
import os
import re
import textwrap
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# Config
# ============================================================
MAX_WORKERS = 8
REQUEST_DELAY_SECONDS = 0.25  # per-domain minimum spacing between requests
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20
CHUNK_SIZE_CHARS = 4000
MAX_FILENAME_CHARS = 90
OUTPUT_DIR = Path.home() / "Documents" / "SEO Crawler" / "seo_output"
USER_AGENT = "SEOAuditCrawler/2.0 (+contact: you@example.com)"
IMAGE_LARGE_BYTES = 1_000_000  # 1 MB heuristic, not a Google rule
IMAGE_NEARBY_TEXT_LIMIT = 350

# Optional heuristic thresholds. These are NOT Google ranking rules.
THIN_CONTENT_WORDS = 250

# Automatic keyword discovery settings.
# Higher weights mean that a term occurring in that HTML location contributes
# more to its page-level term frequency before IDF is applied.
TFIDF_MAX_NGRAM = 3
TFIDF_MIN_DF = 2
TFIDF_TOP_TERMS_PER_PAGE = 25
TFIDF_REPORT_LIMIT = 150

# All keyword-focused reports (per-site TF-IDF terms, the primary top
# keywords, and the competitor keyword-gap report) are grouped together in
# this single subfolder instead of being scattered across the output tree.
KEYWORD_REPORTS_DIRNAME = "keyword_reports"

LOCATION_WEIGHTS = {
    "title": 6.0,
    "h1": 5.0,
    "h2": 4.0,
    "h3": 3.0,
    "h4": 2.5,
    "h5": 2.0,
    "h6": 1.5,
    "meta_description": 2.0,
    "p": 1.0,
    "li": 1.0,
}

STOPWORDS = {
    "the", "and", "or", "a", "an", "to", "of", "in", "on", "for", "with",
    "by", "is", "are", "this", "that", "it", "as", "be", "from", "at", "your",
    "our", "their", "you", "we", "us", "they", "i", "he", "she", "them", "its",
    "was", "were", "will", "can", "may", "not", "have", "has", "had", "do", "does",
    "did", "but", "if", "so", "than", "then", "also", "more", "most", "about",
    "into", "over", "under", "up", "down", "out", "all", "any", "some", "what",
    "when", "where", "who", "how", "why", "which"
}

# ============================================================
# HTTP: pooling, retries, rate limiting
# ============================================================
_thread_local = threading.local()
_domain_lock = threading.Lock()
_domain_last_request = defaultdict(float)
_robots_cache = {}
_robots_lock = threading.Lock()


def get_session():
    """One requests.Session per worker thread."""
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        _thread_local.session = session
    return _thread_local.session


def normalized_host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url):
    """Remove fragments and normalize obvious URL noise without changing semantics."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https"):
        return ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def rate_limit(url, extra_delay=0.0):
    host = normalized_host(url)
    delay = max(REQUEST_DELAY_SECONDS, extra_delay or 0.0)
    with _domain_lock:
        elapsed = time.monotonic() - _domain_last_request[host]
        wait = delay - elapsed
        if wait > 0:
            time.sleep(wait)
        _domain_last_request[host] = time.monotonic()


def get_robots_parser(url):
    """Fetch/cache robots.txt per host. Failure defaults to crawl allowed."""
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    key = robots_url.lower()

    with _robots_lock:
        if key in _robots_cache:
            return _robots_cache[key]

    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        session = get_session()
        rate_limit(robots_url)
        r = session.get(robots_url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if r.ok:
            rp.parse(r.text.splitlines())
        else:
            # Empty rules => allow by default.
            rp.parse([])
    except requests.RequestException:
        rp.parse([])

    with _robots_lock:
        _robots_cache[key] = rp
    return rp


def allowed_by_robots(url):
    rp = get_robots_parser(url)
    return rp.can_fetch(USER_AGENT, url), (rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0)


def fetch(url, check_robots=False):
    if check_robots:
        allowed, crawl_delay = allowed_by_robots(url)
        if not allowed:
            raise PermissionError("Blocked by robots.txt")
    else:
        crawl_delay = 0

    rate_limit(url, crawl_delay)
    return get_session().get(
        url,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )


# ============================================================
# Sitemap discovery + robust parsing
# ============================================================
INVALID_XML_CHARS = re.compile(
    "[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]"
)


def _decode_sitemap_response(resp, url):
    raw = resp.content
    if url.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode(resp.encoding or "utf-8", errors="replace")


def _extract_locs_fallback(xml_text):
    """Fallback for malformed sitemap XML (e.g. bad '&' inside a <loc>)."""
    locs = re.findall(r"<loc\b[^>]*>(.*?)</loc>", xml_text, flags=re.I | re.S)
    cleaned = []
    for loc in locs:
        loc = re.sub(r"<[^>]+>", "", loc)
        loc = html_lib.unescape(loc).strip()
        if loc:
            cleaned.append(loc)
    return cleaned


NON_HTML_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif",
    ".webp", ".svg", ".avif", ".bmp",
    ".ico", ".pdf", ".zip", ".gz",
    ".rar", ".7z", ".mp4", ".webm",
    ".mov", ".avi", ".mp3", ".wav",
    ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx",
}


def is_probable_html_url(url):
    path = urlparse(url).path.lower()

    return not any(
        path.endswith(ext)
        for ext in NON_HTML_EXTENSIONS
    )


def fetch_sitemap_urls(sitemap_url, visited=None):
    if visited is None:
        visited = set()

    sitemap_url = normalize_url(sitemap_url)

    if not sitemap_url or sitemap_url in visited:
        return []

    visited.add(sitemap_url)

    try:
        resp = fetch(sitemap_url, check_robots=False)
        resp.raise_for_status()

        xml_text = _decode_sitemap_response(
            resp,
            sitemap_url
        )

    except Exception as e:
        print(
            f"Failed sitemap: "
            f"{sitemap_url} -> {e}"
        )
        return []

    xml_text = INVALID_XML_CHARS.sub(
        "",
        xml_text
    )

    is_index = (
            "<sitemapindex"
            in xml_text.lower()
    )

    try:
        root = ET.fromstring(
            xml_text.encode("utf-8")
        )

        tag = (
            root.tag
            .split("}")[-1]
            .lower()
        )

        is_index = (
                tag == "sitemapindex"
        )

        locs = []

        # Important:
        # only extract direct <loc> children
        # from <url> or <sitemap>.
        #
        # Ignore:
        # <image:loc>
        # <video:loc>
        # etc.
        expected_parent = (
            "sitemap"
            if is_index
            else "url"
        )

        for parent in root:

            parent_tag = (
                parent.tag
                .split("}")[-1]
                .lower()
            )

            if parent_tag != expected_parent:
                continue

            for child in parent:

                child_tag = (
                    child.tag
                    .split("}")[-1]
                    .lower()
                )

                if (
                        child_tag == "loc"
                        and child.text
                ):
                    locs.append(
                        html_lib.unescape(
                            child.text.strip()
                        )
                    )
                    break

    except ET.ParseError as e:

        print(
            f"XML warning: "
            f"{sitemap_url} -> {e}; "
            f"using tolerant <loc> fallback"
        )

        locs = _extract_locs_fallback(
            xml_text
        )

    # Sitemap index:
    # recursively load child sitemaps
    if is_index:

        all_urls = []

        for child in locs:
            all_urls.extend(
                fetch_sitemap_urls(
                    child,
                    visited
                )
            )

        return list(
            dict.fromkeys(all_urls)
        )

    # Normal sitemap:
    # discard obvious non-HTML assets
    page_urls = []

    for u in locs:

        normalized = normalize_url(u)

        if not normalized:
            continue

        if not is_probable_html_url(
                normalized
        ):
            continue

        page_urls.append(
            normalized
        )

    return list(
        dict.fromkeys(page_urls)
    )


def discover_sitemaps(site_or_sitemap):
    """Accept either a sitemap URL or a site homepage and discover common sitemaps."""
    site_or_sitemap = normalize_url(site_or_sitemap)
    if not site_or_sitemap:
        return []

    path = urlparse(site_or_sitemap).path.lower()
    if path.endswith(".xml") or path.endswith(".xml.gz"):
        return [site_or_sitemap]

    p = urlparse(site_or_sitemap)
    base = f"{p.scheme}://{p.netloc}"
    candidates = []

    # First: sitemap declarations from robots.txt
    try:
        rp = get_robots_parser(base)
        candidates.extend(rp.site_maps() or [])
    except Exception:
        pass

    # Common fallbacks
    candidates.extend([
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/wp-sitemap.xml",
    ])
    return list(dict.fromkeys(normalize_url(x) for x in candidates if normalize_url(x)))


def collect_site_urls(site_or_sitemap):
    for sitemap in discover_sitemaps(site_or_sitemap):
        urls = fetch_sitemap_urls(sitemap)
        if urls:
            print(f"Using sitemap: {sitemap}")
            return urls
    return []


# ============================================================
# HTML/content extraction
# ============================================================
def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def main_content_root(soup):
    return soup.find("main") or soup.find("article") or soup.body or soup


def html_to_markdown(soup):
    """Preserve document order for headings, paragraphs, and list items."""
    root = main_content_root(soup)
    lines = []

    for elem in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        # Avoid duplicating nested list items.
        if elem.name == "li" and elem.find_parent("li"):
            continue
        text = clean_text(elem.get_text(" ", strip=True))
        if not text:
            continue
        if elem.name.startswith("h"):
            level = int(elem.name[1])
            lines.append(f"{'#' * level} {text}")
        elif elem.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    return "\n\n".join(lines).strip()


def visible_text(soup):
    root = main_content_root(soup)
    # Work on a copy so metadata extraction from the original soup is untouched.
    copy = BeautifulSoup(str(root), "html.parser")
    for tag in copy(["script", "style", "noscript", "svg", "template", "form"]):
        tag.decompose()
    return clean_text(copy.get_text(" ", strip=True))


def chunk_markdown(md_text, chunk_size=CHUNK_SIZE_CHARS):
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")

    chunks, current, current_len = [], [], 0
    for block in md_text.split("\n\n"):
        # A single long paragraph must not bypass the chunk size used by LLM
        # clients such as Claude.
        blocks = textwrap.wrap(
            block,
            width=chunk_size,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [block]
        for part in blocks:
            part_len = len(part) + (2 if current else 0)
            if current and current_len + part_len > chunk_size:
                chunks.append("\n\n".join(current).strip())
                current, current_len = [], 0
                part_len = len(part)
            current.append(part)
            current_len += part_len
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


# ============================================================
# Image extraction
# ============================================================
def extract_images(soup, base_url):
    """Extract basic SEO information from images on a page."""
    images = []

    for img in soup.find_all("img"):
        # Common normal/lazy-load image source attributes.
        src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or ""
        )

        # Fall back to the first srcset candidate when no src is available.
        if not src:
            srcset = clean_text(img.get("srcset", ""))
            if srcset:
                first_candidate = srcset.split(",")[0].strip()
                src = first_candidate.split()[0].strip()

        image_url = ""
        if src and not src.lower().startswith("data:"):
            image_url = normalize_url(urljoin(base_url, src))

        alt = clean_text(img.get("alt", ""))
        title = clean_text(img.get("title", ""))
        aria_label = clean_text(img.get("aria-label", ""))
        width = img.get("width", "")
        height = img.get("height", "")
        loading = img.get("loading", "")

        # Nearest heading before the image.
        heading = img.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        nearest_heading = (
            clean_text(heading.get_text(" ", strip=True))
            if heading else ""
        )

        # Figure caption, when present.
        figcaption = ""
        figure = img.find_parent("figure")
        if figure:
            caption = figure.find("figcaption")
            if caption:
                figcaption = clean_text(caption.get_text(" ", strip=True))

        # Small amount of nearby parent text for image/topic context.
        nearby_text = ""
        if img.parent:
            nearby_text = clean_text(img.parent.get_text(" ", strip=True))[:300]

        images.append({
            "image_url": image_url,
            "alt": alt,
            "title": title,
            "aria_label": aria_label,
            "width": width,
            "height": height,
            "loading": loading,
            "nearest_heading": nearest_heading,
            "figcaption": figcaption,
            "nearby_text": nearby_text,
            "missing_alt": not bool(alt),
        })

    return images


# ============================================================
# Automatic keyword extraction: location-weighted TF-IDF
# ============================================================
def _keyword_tokens(text):
    """Tokenize text for SEO keyword candidates without stemming.

    We intentionally preserve natural words instead of stems because the output
    is meant to be readable as SEO terms/phrases.
    """
    return re.findall(r"[a-z][a-z0-9'-]*", (text or "").lower())


def generate_terms(text, max_ngram=TFIDF_MAX_NGRAM):
    """Generate readable 1..N-gram keyword candidates.

    Unigram stopwords are removed. For phrases, stopwords may appear inside a
    phrase (e.g. "security for business"), but a phrase cannot start or end
    with a stopword. This avoids many low-value phrase candidates.
    """
    # Split on sentence/strong punctuation boundaries first so phrases are not
    # accidentally created across separate sentences (e.g. "security. Contact").
    segments = re.split(r"[.!?;:\n\r]+", text or "")
    terms = []
    max_ngram = max(1, int(max_ngram))

    for segment in segments:
        tokens = _keyword_tokens(segment)
        if not tokens:
            continue

        for n in range(1, max_ngram + 1):
            for i in range(0, len(tokens) - n + 1):
                parts = tokens[i:i + n]

                if n == 1:
                    if parts[0] in STOPWORDS or len(parts[0]) < 3:
                        continue
                else:
                    if parts[0] in STOPWORDS or parts[-1] in STOPWORDS:
                        continue
                    if all(part in STOPWORDS for part in parts):
                        continue

                term = " ".join(parts)
                if len(term) > 90:
                    continue
                terms.append(term)

    return terms


def extract_location_weighted_terms(soup, title, meta_description, max_ngram=TFIDF_MAX_NGRAM):
    """Return weighted term counts based on where terms occur on the page.

    Example with the default weights:
        one H1 occurrence = 5 weighted occurrences
        one H2 occurrence = 4 weighted occurrences
        one paragraph occurrence = 1 weighted occurrence

    TF-IDF is applied later across the entire primary + competitor corpus so
    every site is scored against the same IDF scale.
    """
    weighted = Counter()
    location_counts = defaultdict(Counter)

    def add_text(text, location):
        text = clean_text(text)
        if not text:
            return
        weight = float(LOCATION_WEIGHTS.get(location, 1.0))
        terms = generate_terms(text, max_ngram=max_ngram)
        counts = Counter(terms)
        for term, count in counts.items():
            weighted[term] += count * weight
            location_counts[location][term] += count

    add_text(title, "title")
    add_text(meta_description, "meta_description")

    root = main_content_root(soup)
    for elem in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        # Avoid nested list duplication.
        if elem.name == "li" and elem.find_parent("li"):
            continue
        # Avoid counting paragraph text twice when it lives inside a list item.
        if elem.name == "p" and elem.find_parent("li"):
            continue

        text = clean_text(elem.get_text(" ", strip=True))
        if text:
            add_text(text, elem.name)

    return dict(weighted), {
        location: dict(counts)
        for location, counts in location_counts.items()
    }


def extract_schema_types(soup):
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            t = obj.get("@type")
            if isinstance(t, str):
                found.add(t)
            elif isinstance(t, list):
                found.update(str(x) for x in t)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        try:
            walk(json.loads(tag.string or tag.get_text() or "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(found)


def extract_links(soup, base_url):
    base_host = normalized_host(base_url)
    internal, external = set(), set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if not absolute:
            continue
        if normalized_host(absolute) == base_host:
            internal.add(absolute)
        else:
            external.add(absolute)
    return sorted(internal), sorted(external)


def build_audit_issues(data):
    issues = []
    if not data.get("title"):
        issues.append("missing_title")
    if not data.get("meta_description"):
        issues.append("missing_meta_description")
    if data.get("h1_count", 0) == 0:
        issues.append("missing_h1")
    if data.get("h1_count", 0) > 1:
        issues.append("multiple_h1_review")
    if data.get("noindex"):
        issues.append("noindex")
    if data.get("canonical") and normalize_url(data["canonical"]) != normalize_url(data["final_url"]):
        issues.append("canonical_points_elsewhere")
    if data.get("word_count", 0) < THIN_CONTENT_WORDS:
        issues.append("thin_content_review")
    if data.get("images_missing_alt", 0) > 0:
        issues.append("images_missing_alt")
    return issues


# ============================================================
# Page processing
# ============================================================
def process_page(url, max_ngram=TFIDF_MAX_NGRAM):
    started = time.perf_counter()

    try:
        resp = fetch(url, check_robots=True)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
    except PermissionError as e:
        return {"url": url, "error": str(e), "robots_blocked": True}
    except requests.RequestException as e:
        return {"url": url, "error": str(e)}

    content_type = (resp.headers.get("content-type") or "").lower()
    result = {
        "url": url,
        "final_url": normalize_url(resp.url),
        "status_code": resp.status_code,
        "redirect_count": len(resp.history),
        "response_ms": elapsed_ms,
        "content_type": content_type,
        "content_bytes": len(resp.content),
        "error": "",
        "robots_blocked": False,
    }

    if resp.status_code >= 400:
        result["error"] = f"HTTP {resp.status_code}"
        return result

    if "html" not in content_type and "xhtml" not in content_type:
        result["error"] = "Non-HTML response"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""

    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_description = clean_text(desc_tag.get("content", "")) if desc_tag else ""

    canonical_tag = soup.find(
        "link",
        rel=lambda v: v and "canonical" in [x.lower() for x in (v if isinstance(v, list) else [v])],
    )
    canonical = (
        normalize_url(urljoin(resp.url, canonical_tag.get("href", "")))
        if canonical_tag and canonical_tag.get("href")
        else ""
    )

    robots_values = []
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}):
        robots_values.append(meta.get("content", ""))
    robots_meta = ", ".join(x for x in robots_values if x)
    noindex = "noindex" in robots_meta.lower()

    h1s = [
        clean_text(h.get_text(" ", strip=True))
        for h in soup.find_all("h1")
        if clean_text(h.get_text(" ", strip=True))
    ]
    h2s = [
        clean_text(h.get_text(" ", strip=True))
        for h in soup.find_all("h2")
        if clean_text(h.get_text(" ", strip=True))
    ]

    text = visible_text(soup)
    md = html_to_markdown(soup)
    chunks = chunk_markdown(md)
    words = re.findall(r"\b[\w'-]+\b", text.lower())

    internal_links, external_links = extract_links(soup, resp.url)

    image_details = extract_images(soup, resp.url)
    images_missing_alt = sum(
        1 for img in image_details if img.get("missing_alt")
    )

    weighted_terms, term_location_counts = extract_location_weighted_terms(
        soup,
        title,
        meta_description,
        max_ngram=max_ngram,
    )

    result.update({
        "title": title,
        "title_length": len(title),
        "meta_description": meta_description,
        "meta_description_length": len(meta_description),
        "canonical": canonical,
        "robots_meta": robots_meta,
        "noindex": noindex,
        "lang": (soup.html.get("lang", "") if soup.html else ""),
        "h1_count": len(h1s),
        "h1_text": h1s,
        "h2_count": len(h2s),
        "word_count": len(words),
        "internal_outlinks_count": len(internal_links),
        "external_outlinks_count": len(external_links),
        "internal_links": internal_links,
        "external_links": external_links,
        "image_count": len(image_details),
        "images_missing_alt": images_missing_alt,
        "images": image_details,
        "schema_types": extract_schema_types(soup),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        # Internal analysis data. Removed from compact JSON after TF-IDF is calculated.
        "weighted_term_counts": weighted_terms,
        "term_location_counts": term_location_counts,
        "markdown": md,
        "chunks": chunks,
    })
    result["audit_issues"] = build_audit_issues(result)
    return result


# ============================================================
# Site-level audit metrics
# ============================================================
def add_site_level_metrics(results):
    successful = [r for r in results if not r.get("error") and r.get("final_url")]
    crawled_urls = {normalize_url(r["final_url"]) for r in successful}

    inlinks = Counter()
    for r in successful:
        for link in r.get("internal_links", []):
            link = normalize_url(link)
            if link in crawled_urls:
                inlinks[link] += 1

    title_map = defaultdict(list)
    desc_map = defaultdict(list)
    hash_map = defaultdict(list)

    for r in successful:
        if r.get("title"):
            title_map[r["title"].lower()].append(r["final_url"])
        if r.get("meta_description"):
            desc_map[r["meta_description"].lower()].append(r["final_url"])
        if r.get("content_hash"):
            hash_map[r["content_hash"]].append(r["final_url"])

    for r in successful:
        final = normalize_url(r["final_url"])
        r["internal_inlinks_count"] = inlinks[final]
        r["duplicate_title"] = (
            len(title_map.get(r.get("title", "").lower(), [])) > 1
            if r.get("title") else False
        )
        r["duplicate_meta_description"] = (
            len(desc_map.get(r.get("meta_description", "").lower(), [])) > 1
            if r.get("meta_description") else False
        )
        r["duplicate_content"] = (
            len(hash_map.get(r.get("content_hash", ""), [])) > 1
            if r.get("content_hash") else False
        )

        if r["duplicate_title"]:
            r["audit_issues"].append("duplicate_title")
        if r["duplicate_meta_description"]:
            r["audit_issues"].append("duplicate_meta_description")
        if r["duplicate_content"]:
            r["audit_issues"].append("duplicate_content")
        if r["internal_inlinks_count"] == 0:
            r["audit_issues"].append("zero_internal_inlinks_review")

    return results


# ============================================================
# Corpus-level weighted TF-IDF
# ============================================================
def compute_weighted_tfidf(result_sets, min_df=TFIDF_MIN_DF):
    """Compute TF-IDF once across primary + all competitors.

    Formula:
        weighted_count(term, page)
            = sum(location_count * location_weight)

        TF(term, page)
            = weighted_count / sum(all weighted term counts on that page)

        IDF(term)
            = log((1 + N) / (1 + document_frequency)) + 1

        TF-IDF
            = TF * IDF

    Computing a single global IDF makes primary/competitor scores directly
    comparable.
    """
    docs = [
        r
        for results in result_sets
        for r in results
        if not r.get("error") and r.get("weighted_term_counts")
    ]

    if not docs:
        return {}, 0

    n_docs = len(docs)
    effective_min_df = max(1, min(int(min_df), n_docs))

    doc_frequency = Counter()
    for r in docs:
        doc_frequency.update(r["weighted_term_counts"].keys())

    allowed_terms = {
        term
        for term, df in doc_frequency.items()
        if df >= effective_min_df
    }

    idf = {
        term: math.log((1 + n_docs) / (1 + doc_frequency[term])) + 1.0
        for term in allowed_terms
    }

    for r in docs:
        counts = r.get("weighted_term_counts", {})
        filtered_counts = {
            term: float(count)
            for term, count in counts.items()
            if term in allowed_terms
        }

        total_weighted = sum(filtered_counts.values())
        if total_weighted <= 0:
            r["tfidf_scores"] = {}
            r["top_tfidf_terms"] = []
            continue

        scores = {
            term: (count / total_weighted) * idf[term]
            for term, count in filtered_counts.items()
        }

        ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        r["tfidf_scores"] = scores
        r["top_tfidf_terms"] = [
            {
                "term": term,
                "score": round(score, 8),
                "weighted_count": round(filtered_counts[term], 3),
            }
            for term, score in ordered[:TFIDF_TOP_TERMS_PER_PAGE]
        ]

    return idf, effective_min_df


def aggregate_site_tfidf(results):
    """Aggregate page-level TF-IDF into a site-level term profile."""
    ok = [r for r in results if not r.get("error") and r.get("tfidf_scores") is not None]
    n_pages = max(1, len(ok))

    score_sum = Counter()
    page_df = Counter()
    weighted_sum = Counter()

    for r in ok:
        scores = r.get("tfidf_scores", {})
        score_sum.update(scores)
        page_df.update(scores.keys())
        weighted_sum.update(r.get("weighted_term_counts", {}))

    profile = {}
    for term in score_sum:
        profile[term] = {
            "avg_tfidf": score_sum[term] / n_pages,
            "page_coverage": page_df[term] / n_pages,
            "weighted_mentions_per_page": weighted_sum[term] / n_pages,
            "pages_using": page_df[term],
        }
    return profile, len(ok)


def build_tfidf_gap_report(primary_results, competitor_result_sets, idf):
    """Find terms competitors emphasize more strongly than the primary site.

    Each competitor site is weighted equally rather than letting a competitor
    with hundreds of pages dominate a smaller competitor.
    """
    primary_profile, primary_pages = aggregate_site_tfidf(primary_results)

    competitor_profiles = []
    competitor_page_counts = []
    for results in competitor_result_sets:
        profile, page_count = aggregate_site_tfidf(results)
        competitor_profiles.append(profile)
        competitor_page_counts.append(page_count)

    if not competitor_profiles:
        # No competitors: produce the primary site's strongest automatically
        # discovered terms instead of an opportunity/gap list.
        rows = []
        for term, metrics in primary_profile.items():
            rows.append({
                "term": term,
                "idf": round(idf.get(term, 0.0), 6),
                "primary_avg_tfidf": round(metrics["avg_tfidf"], 8),
                "competitor_avg_tfidf": 0.0,
                "tfidf_gap": round(-metrics["avg_tfidf"], 8),
                "primary_page_coverage": round(metrics["page_coverage"], 4),
                "competitor_avg_page_coverage": 0.0,
                "coverage_gap": round(-metrics["page_coverage"], 4),
                "competitor_sites_using": 0,
                "primary_weighted_mentions_per_page": round(metrics["weighted_mentions_per_page"], 4),
                "competitor_avg_weighted_mentions_per_page": 0.0,
                "opportunity_score": 0.0,
            })
        rows.sort(key=lambda x: x["primary_avg_tfidf"], reverse=True)
        return rows

    all_terms = set(primary_profile)
    for profile in competitor_profiles:
        all_terms.update(profile)

    rows = []
    competitor_count = len(competitor_profiles)

    for term in all_terms:
        p = primary_profile.get(term, {})
        p_score = float(p.get("avg_tfidf", 0.0))
        p_cov = float(p.get("page_coverage", 0.0))
        p_mentions = float(p.get("weighted_mentions_per_page", 0.0))

        c_scores = []
        c_covs = []
        c_mentions = []
        sites_using = 0

        for profile in competitor_profiles:
            c = profile.get(term, {})
            score = float(c.get("avg_tfidf", 0.0))
            cov = float(c.get("page_coverage", 0.0))
            mentions = float(c.get("weighted_mentions_per_page", 0.0))
            c_scores.append(score)
            c_covs.append(cov)
            c_mentions.append(mentions)
            if cov > 0:
                sites_using += 1

        c_score = sum(c_scores) / competitor_count
        c_cov = sum(c_covs) / competitor_count
        c_mentions_avg = sum(c_mentions) / competitor_count

        tfidf_gap = c_score - p_score
        coverage_gap = c_cov - p_cov

        # Only surface genuine competitor opportunities in the gap report.
        if tfidf_gap <= 0:
            continue

        # TF-IDF difference is the foundation. Coverage and agreement across
        # competitor sites increase confidence that it is a meaningful topic.
        competitor_agreement = sites_using / competitor_count
        opportunity = (
                tfidf_gap
                * (1.0 + max(0.0, coverage_gap))
                * (0.75 + 0.25 * competitor_agreement)
        )

        rows.append({
            "term": term,
            "idf": round(idf.get(term, 0.0), 6),
            "primary_avg_tfidf": round(p_score, 8),
            "competitor_avg_tfidf": round(c_score, 8),
            "tfidf_gap": round(tfidf_gap, 8),
            "primary_page_coverage": round(p_cov, 4),
            "competitor_avg_page_coverage": round(c_cov, 4),
            "coverage_gap": round(coverage_gap, 4),
            "competitor_sites_using": sites_using,
            "primary_weighted_mentions_per_page": round(p_mentions, 4),
            "competitor_avg_weighted_mentions_per_page": round(c_mentions_avg, 4),
            "opportunity_score": round(opportunity, 8),
        })

    rows.sort(
        key=lambda x: (
            x["opportunity_score"],
            x["competitor_avg_tfidf"],
            x["competitor_avg_page_coverage"],
        ),
        reverse=True,
    )
    return rows


def build_keyword_gap_report_json(
        primary,
        competitors,
        primary_results,
        gap_rows,
        primary_term_rows,
        min_df,
        max_ngram,
        report_limit=TFIDF_REPORT_LIMIT,
):
    """Consolidate the keyword outputs into one JSON handoff file.

    This is the single artifact meant to be fed to Claude (manually or via an
    MCP/automation loop) for keyword filtering and downstream keyword.com
    lookups. It mirrors the schema referenced throughout the Claude prompt
    pack (`keyword_gap_report.json`).
    """
    limit = max(1, int(report_limit))
    missing_keywords = [row for row in gap_rows if row.get("primary_page_coverage", 0) == 0]
    strengthen_keywords = [row for row in gap_rows if row.get("primary_page_coverage", 0) > 0]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_site": primary,
        "competitor_sites": list(competitors),
        "pages_crawled_primary": len(primary_results),
        "tfidf_settings": {
            "min_document_frequency": min_df,
            "max_ngram": max_ngram,
        },
        "primary_top_keywords": primary_term_rows[:limit],
        "missing_keywords": missing_keywords[:limit],
        "keywords_to_strengthen": strengthen_keywords[:limit],
        "keyword_gap_full": gap_rows[:limit],
    }

    with open(OUTPUT_DIR / "keyword_gap_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def build_site_term_rows(results, idf):
    profile, _ = aggregate_site_tfidf(results)
    rows = []
    for term, metrics in profile.items():
        rows.append({
            "term": term,
            "idf": round(idf.get(term, 0.0), 6),
            "avg_tfidf": round(metrics["avg_tfidf"], 8),
            "page_coverage": round(metrics["page_coverage"], 4),
            "pages_using": metrics["pages_using"],
            "weighted_mentions_per_page": round(metrics["weighted_mentions_per_page"], 4),
        })
    rows.sort(key=lambda x: (x["avg_tfidf"], x["page_coverage"]), reverse=True)
    return rows


# ============================================================
# Output helpers
# ============================================================
def safe_filename(url):
    p = urlparse(url)
    base = f"{p.netloc}{p.path}"
    if p.query:
        base += "_" + p.query
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    # Keep names short so full paths stay under the Windows 260-char limit.
    if len(base) > MAX_FILENAME_CHARS:
        digest = hashlib.md5(url.encode()).hexdigest()[:10]
        base = base[:MAX_FILENAME_CHARS - 11].rstrip("_.-") + "_" + digest
    return base or "home"


def _format_top_terms(r, limit=10):
    return " | ".join(
        f"{item['term']} ({item['score']:.5f})"
        for item in r.get("top_tfidf_terms", [])[:limit]
    )


def flatten_for_csv(r):
    return {
        "url": r.get("url", ""),
        "final_url": r.get("final_url", ""),
        "status_code": r.get("status_code", ""),
        "redirect_count": r.get("redirect_count", ""),
        "response_ms": r.get("response_ms", ""),
        "content_type": r.get("content_type", ""),
        "content_bytes": r.get("content_bytes", ""),
        "title": r.get("title", ""),
        "title_length": r.get("title_length", ""),
        "meta_description": r.get("meta_description", ""),
        "meta_description_length": r.get("meta_description_length", ""),
        "canonical": r.get("canonical", ""),
        "robots_meta": r.get("robots_meta", ""),
        "noindex": r.get("noindex", ""),
        "lang": r.get("lang", ""),
        "h1_count": r.get("h1_count", ""),
        "h1_text": " | ".join(r.get("h1_text", [])),
        "h2_count": r.get("h2_count", ""),
        "word_count": r.get("word_count", ""),
        "internal_inlinks_count": r.get("internal_inlinks_count", ""),
        "internal_outlinks_count": r.get("internal_outlinks_count", ""),
        "external_outlinks_count": r.get("external_outlinks_count", ""),
        "image_count": r.get("image_count", ""),
        "images_missing_alt": r.get("images_missing_alt", ""),
        "schema_types": " | ".join(r.get("schema_types", [])),
        "top_tfidf_terms": _format_top_terms(r),
        "duplicate_title": r.get("duplicate_title", ""),
        "duplicate_meta_description": r.get("duplicate_meta_description", ""),
        "duplicate_content": r.get("duplicate_content", ""),
        "audit_issues": " | ".join(r.get("audit_issues", [])),
        "error": r.get("error", ""),
    }


def flatten_images_for_csv(results):
    rows = []

    for page in results:
        page_url = page.get("final_url") or page.get("url", "")

        for img in page.get("images", []):
            rows.append({
                "page_url": page_url,
                "image_url": img.get("image_url", ""),
                "alt": img.get("alt", ""),
                "title": img.get("title", ""),
                "aria_label": img.get("aria_label", ""),
                "width": img.get("width", ""),
                "height": img.get("height", ""),
                "loading": img.get("loading", ""),
                "nearest_heading": img.get("nearest_heading", ""),
                "figcaption": img.get("figcaption", ""),
                "nearby_text": img.get("nearby_text", ""),
                "missing_alt": img.get("missing_alt", False),
            })

    return rows


def save_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def save_site_outputs(site_label, results, idf):
    site_dir = OUTPUT_DIR / site_label
    md_dir = site_dir / "markdown"
    chunk_dir = site_dir / "chunks"
    keyword_reports_dir = OUTPUT_DIR / KEYWORD_REPORTS_DIRNAME
    site_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(exist_ok=True)
    chunk_dir.mkdir(exist_ok=True)
    keyword_reports_dir.mkdir(parents=True, exist_ok=True)

    save_csv(site_dir / "pages.csv", [flatten_for_csv(r) for r in results])
    save_csv(site_dir / "images.csv", flatten_images_for_csv(results))
    save_csv(
        keyword_reports_dir / f"{site_label}_tfidf_terms.csv",
        build_site_term_rows(results, idf),
    )

    # JSON retains useful nested data but omits the potentially huge internal
    # term-count dictionaries used only during TF-IDF computation.
    json_results = []
    for r in results:
        compact = {
            k: v
            for k, v in r.items()
            if k not in (
                "markdown",
                "chunks",
                "weighted_term_counts",
                "term_location_counts",
                "tfidf_scores",
            )
        }
        json_results.append(compact)

    with open(site_dir / "pages.json", "w", encoding="utf-8") as f:
        json.dump(json_results, f, ensure_ascii=False, indent=2)

    # Markdown/chunks for LLM analysis.
    for r in results:
        if not r.get("markdown"):
            continue
        name = safe_filename(r.get("final_url") or r["url"])
        with open(md_dir / f"{name}.md", "w", encoding="utf-8") as f:
            f.write(f"URL: {r.get('final_url') or r['url']}\n\n{r['markdown']}")

        page_chunk_dir = chunk_dir / name
        page_chunk_dir.mkdir(exist_ok=True)
        for i, chunk in enumerate(r.get("chunks", []), 1):
            with open(page_chunk_dir / f"chunk_{i}.md", "w", encoding="utf-8") as f:
                f.write(f"URL: {r.get('final_url') or r['url']}\nChunk: {i}\n\n{chunk}")

    summary = {
        "pages_total": len(results),
        "pages_successful": sum(1 for r in results if not r.get("error")),
        "pages_with_errors": sum(1 for r in results if r.get("error")),
        "noindex_pages": sum(1 for r in results if r.get("noindex")),
        "missing_titles": sum(1 for r in results if not r.get("error") and not r.get("title")),
        "missing_meta_descriptions": sum(1 for r in results if not r.get("error") and not r.get("meta_description")),
        "duplicate_titles": sum(1 for r in results if r.get("duplicate_title")),
        "duplicate_content_pages": sum(1 for r in results if r.get("duplicate_content")),
    }
    with open(site_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return site_dir


def save_tfidf_config(min_df, max_ngram):
    config = {
        "method": "location-weighted TF-IDF",
        "formula": {
            "weighted_count": "sum(location_occurrences * location_weight)",
            "tf": "weighted_count / total_weighted_counts_on_page",
            "idf": "ln((1 + total_documents) / (1 + document_frequency)) + 1",
            "tfidf": "tf * idf",
        },
        "location_weights": LOCATION_WEIGHTS,
        "min_document_frequency": min_df,
        "max_ngram": max_ngram,
        "top_terms_per_page": TFIDF_TOP_TERMS_PER_PAGE,
    }
    with open(OUTPUT_DIR / "tfidf_config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ============================================================
# Crawling pipeline
# ============================================================
def crawl_site(site_label, site_or_sitemap, max_ngram=TFIDF_MAX_NGRAM):
    print(f"\n=== Processing {site_label}: {site_or_sitemap} ===")
    urls = collect_site_urls(site_or_sitemap)
    print(f"Found {len(urls)} sitemap URLs")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_page, url, max_ngram): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"url": url, "error": f"Unhandled: {e}"}
            results.append(result)
            state = "OK" if not result.get("error") else f"ERROR: {result.get('error')}"
            print(f"[{state}] {url}")

    add_site_level_metrics(results)
    return results


def build_ai_audit_packet(
        primary_results,
        gap_rows,
        primary_term_rows,
        max_pages=50,
        max_keyword_gaps=30,
        max_primary_terms=20,
):
    """
    Compress crawler results into a token-efficient text packet
    for an AI SEO agent.
    """

    # -------------------------------------------------
    # 1. Give SEO issues different importance weights
    # -------------------------------------------------
    ISSUE_WEIGHTS = {
        "noindex": 10,
        "missing_title": 9,
        "canonical_points_elsewhere": 8,
        "missing_h1": 7,
        "duplicate_content": 7,
        "duplicate_title": 6,
        "zero_internal_inlinks_review": 6,
        "missing_meta_description": 4,
        "thin_content_review": 3,
        "multiple_h1_review": 2,
        "images_missing_alt": 1,
    }

    # -------------------------------------------------
    # 2. Score each page
    # -------------------------------------------------
    def page_score(page):
        score = 0

        for issue in page.get("audit_issues", []):
            score += ISSUE_WEIGHTS.get(issue, 0)

        status = page.get("status_code", 200)

        if isinstance(status, int):
            if status >= 500:
                score += 15
            elif status >= 400:
                score += 12
            elif status >= 300:
                score += 4

        # Pages with fewer internal links deserve review
        inlinks = page.get("internal_inlinks_count", 0)

        if isinstance(inlinks, int):
            if inlinks == 0:
                score += 5
            elif inlinks <= 2:
                score += 2

        return score

    # -------------------------------------------------
    # 3. Keep successful HTML pages
    # -------------------------------------------------
    pages = [
        page
        for page in primary_results
        if not page.get("error")
    ]

    pages.sort(
        key=page_score,
        reverse=True
    )

    pages = pages[:max_pages]

    # -------------------------------------------------
    # 4. Compact page table
    # -------------------------------------------------
    page_lines = [
        "URL|SC|TITLE|TL|META|H1|WORDS|IN|OUT|MS|SCHEMA|ISSUES"
    ]

    for page in pages:
        title = (
            page.get("title", "")
            .replace("|", "/")
            .replace("\n", " ")
        )

        schema = ",".join(
            page.get("schema_types", [])
        )

        issues = ",".join(
            page.get("audit_issues", [])
        )

        page_lines.append("|".join([
            str(page.get("final_url") or page.get("url", "")),
            str(page.get("status_code", "")),
            title,
            str(page.get("title_length", "")),
            str(page.get("meta_description_length", "")),
            str(page.get("h1_count", "")),
            str(page.get("word_count", "")),
            str(page.get("internal_inlinks_count", "")),
            str(page.get("internal_outlinks_count", "")),
            str(page.get("response_ms", "")),
            schema,
            issues,
        ]))

    # -------------------------------------------------
    # 5. Compress keyword gaps
    # -------------------------------------------------
    gap_lines = [
        "TERM|OPP|YOUR_TFIDF|COMP_TFIDF|GAP|YOUR_COV|COMP_COV|SITES"
    ]

    for row in gap_rows[:max_keyword_gaps]:
        gap_lines.append("|".join([
            str(row.get("term", "")),
            str(row.get("opportunity_score", "")),
            str(row.get("primary_avg_tfidf", "")),
            str(row.get("competitor_avg_tfidf", "")),
            str(row.get("tfidf_gap", "")),
            str(row.get("primary_page_coverage", "")),
            str(row.get("competitor_avg_page_coverage", "")),
            str(row.get("competitor_sites_using", "")),
        ]))

    # -------------------------------------------------
    # 6. Compress current primary keywords
    # -------------------------------------------------
    keyword_lines = [
        "TERM|TFIDF|COVERAGE|PAGES|MENTIONS"
    ]

    for row in primary_term_rows[:max_primary_terms]:
        keyword_lines.append("|".join([
            str(row.get("term", "")),
            str(row.get("avg_tfidf", "")),
            str(row.get("page_coverage", "")),
            str(row.get("pages_using", "")),
            str(row.get("weighted_mentions_per_page", "")),
        ]))

    # -------------------------------------------------
    # 7. Create final AI packet
    # -------------------------------------------------
    packet = f"""
        <page_metrics>
        {"\n".join(page_lines)}
        </page_metrics>

        <keyword_gaps>
        {"\n".join(gap_lines)}
        </keyword_gaps>

        <primary_topics>
        {"\n".join(keyword_lines)}
        </primary_topics>
        """.strip()

    return packet


def run_multi_site_pipeline(
        primary,
        competitors,
        *,
        max_ngram=TFIDF_MAX_NGRAM,
        min_df=TFIDF_MIN_DF,
        report_limit=TFIDF_REPORT_LIMIT,
):
    """Crawl sites and automatically discover keyword opportunities.

    No target-keyword list is required. Terms and phrases are discovered from
    the crawled corpus with location-weighted TF-IDF.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    primary_results = crawl_site("primary", primary, max_ngram=max_ngram)

    competitor_sets = []
    for i, competitor in enumerate(competitors, 1):
        competitor_sets.append(
            crawl_site(f"competitor_{i}", competitor, max_ngram=max_ngram)
        )

    all_sets = [primary_results] + competitor_sets
    idf, effective_min_df = compute_weighted_tfidf(all_sets, min_df=min_df)

    print(
        f"\nTF-IDF corpus: {sum(len([r for r in s if not r.get('error')]) for s in all_sets)} "
        f"successful pages | {len(idf)} candidate terms | min_df={effective_min_df}"
    )

    # Save site data after global TF-IDF has been attached to every page.
    save_site_outputs("primary", primary_results, idf)
    for i, results in enumerate(competitor_sets, 1):
        save_site_outputs(f"competitor_{i}", results, idf)

    keyword_reports_dir = OUTPUT_DIR / KEYWORD_REPORTS_DIRNAME
    keyword_reports_dir.mkdir(parents=True, exist_ok=True)

    gap_rows = build_tfidf_gap_report(primary_results, competitor_sets, idf)
    save_csv(
        keyword_reports_dir / "tfidf_keyword_gap.csv",
        gap_rows[:max(1, int(report_limit))],
    )

    # Also save the strongest primary terms alongside the other keyword reports.
    primary_term_rows = build_site_term_rows(primary_results, idf)
    save_csv(
        keyword_reports_dir / "primary_top_keywords.csv",
        primary_term_rows[:max(1, int(report_limit))],
    )

    save_tfidf_config(effective_min_df, max_ngram)

    build_keyword_gap_report_json(
        primary,
        competitors,
        primary_results,
        gap_rows,
        primary_term_rows,
        min_df=effective_min_df,
        max_ngram=max_ngram,
        report_limit=report_limit,
    )

    print(f"\nDone. Reports are in: {OUTPUT_DIR.resolve()}")
    print(f"Keyword reports are grouped in: {keyword_reports_dir.resolve()}")
    print(f"  - {keyword_reports_dir / 'primary_top_keywords.csv'}")
    if competitor_sets:
        print(f"  - {keyword_reports_dir / 'tfidf_keyword_gap.csv'}")
    for site_label in ["primary"] + [f"competitor_{i}" for i in range(1, len(competitor_sets) + 1)]:
        print(f"  - {keyword_reports_dir / f'{site_label}_tfidf_terms.csv'}")
    print(f"  - {OUTPUT_DIR / 'keyword_gap_report.json'} (single-file handoff for Claude)")
    ai_packet = build_ai_audit_packet(
        primary_results,
        gap_rows,
        primary_term_rows,
        max_pages=50,
        max_keyword_gaps=30,
        max_primary_terms=20,
    )

    ai_packet_path = OUTPUT_DIR / "ai_audit_packet.txt"

    with open(
            ai_packet_path,
            "w",
            encoding="utf-8"
    ) as f:
        f.write(ai_packet)

    print(
        f"AI audit packet: {ai_packet_path}"
    )


if __name__ == "__main__":
    primary_site = "https://example.com"
    competitor_sites = [
        "https://competitor1.com",
        "https://competitor2.com",
    ]

    run_multi_site_pipeline(
        primary_site,
        competitor_sites,
        max_ngram=3,
        min_df=2,
        report_limit=150,
    )
