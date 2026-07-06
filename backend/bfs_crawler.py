# =============================================================================
# bfs_crawler.py - Case 3 fallback: BFS link discovery (depth 2, no sitemap/feed)
# Ported from notebook Cell 9 (Case 3 pipeline) and adapted to reuse this
# project's existing async fetch engine + config instead of duplicating it.
#
# When a site has neither an XML sitemap nor an RSS/Atom feed, this module
# does a breadth-first crawl rooted at the seed URL (depth 0 = seed itself,
# depth 1 = links found on it, depth 2 = links found on depth-1 pages) and
# returns the same-domain page URLs it discovered. Those candidate URLs are
# then handed to async_engine.crawl_articles(), exactly like the sitemap and
# RSS pipelines already do, so date extraction / article classification /
# summarization all stay consistent across all three cases.
# =============================================================================
import asyncio
import logging
from urllib.parse import urlparse, urldefrag, urljoin

import aiohttp
from bs4 import BeautifulSoup

from backend.config import (
    REQUEST_HEADERS, REQUEST_TIMEOUT_SECONDS, MAX_CONCURRENT_REQUESTS,
    NON_PAGE_EXTENSIONS, TRACKING_PARAMS,
)

log = logging.getLogger(__name__)

# Extra path keywords that are almost never articles -- skip to save budget.
SKIP_URL_KEYWORDS = (
    "/tag/", "/tags/", "/category/", "/categories/", "/author/",
    "/login", "/signup", "/register", "/cart", "/checkout",
    "/wp-admin", "/wp-login", "/privacy", "/terms", "/cookie",
    "/search", "?s=", "/page/", "/events", "/videos",
)

MAX_BFS_PAGES = 150
BFS_MAX_DEPTH = 2


def normalize_url(u, base=None):
    if base:
        u = urljoin(base, u)
    u, _ = urldefrag(u)
    try:
        parsed = urlparse(u)
        if parsed.query:
            kept = [
                kv for kv in parsed.query.split("&")
                if kv.split("=")[0] not in TRACKING_PARAMS
            ]
            u = parsed._replace(query="&".join(kept)).geturl()
    except Exception:
        pass
    return u.rstrip("/") if u.count("/") > 2 else u


def _extract_links(html, base_url):
    """Pull same-domain, article-shaped links out of a page's HTML."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    domain = urlparse(base_url).netloc
    links = []
    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"], base=base_url)
        if not href.startswith("http"):
            continue
        if urlparse(href).netloc != domain:
            continue
        low = href.lower()
        if low.endswith(NON_PAGE_EXTENSIONS):
            continue
        if any(kw in low for kw in SKIP_URL_KEYWORDS):
            continue
        links.append(href)
    return links


async def _fetch_many(urls):
    """Fetch a batch of URLs concurrently, HTML-only, best-effort (no retries --
    this is just link discovery, not the final article fetch)."""
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    results = {}

    async def _one(session, url):
        async with semaphore:
            try:
                async with session.get(
                    url, headers=REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        ctype = resp.headers.get("Content-Type", "")
                        if "text/html" in ctype or "application/xhtml" in ctype or ctype == "":
                            results[url] = await resp.text(errors="ignore")
            except Exception:
                pass

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[_one(session, u) for u in urls])
    return results


async def _run_bfs(start_url, max_depth=BFS_MAX_DEPTH, max_pages=MAX_BFS_PAGES):
    visited = set()
    frontier = [normalize_url(start_url)]
    discovered = []
    depth = 0

    while frontier and depth <= max_depth and len(visited) < max_pages:
        frontier = [u for u in frontier if u not in visited]
        if not frontier:
            break
        remaining = max_pages - len(visited)
        frontier = frontier[:remaining]

        log.info("BFS Case 3: crawling depth %d -- %d page(s) ...", depth, len(frontier))
        fetched = await _fetch_many(frontier)

        next_frontier = []
        for url in frontier:
            visited.add(url)
            html = fetched.get(url)
            if not html:
                continue
            discovered.append(url)
            if depth < max_depth:
                next_frontier.extend(_extract_links(html, url))

        frontier = list(dict.fromkeys(next_frontier))
        depth += 1

    log.info("BFS Case 3: %d page(s) discovered across depth 0-%d.", len(discovered), max_depth)
    return discovered


def discover_bfs_candidates(start_url, max_depth=BFS_MAX_DEPTH, max_pages=MAX_BFS_PAGES):
    """Synchronous wrapper -- safe to call from a background thread.
    Returns a de-duplicated list of same-domain page URLs found via BFS."""
    loop = asyncio.new_event_loop()
    try:
        pages = loop.run_until_complete(_run_bfs(start_url, max_depth, max_pages))
    finally:
        loop.close()
    return list(dict.fromkeys(pages))
