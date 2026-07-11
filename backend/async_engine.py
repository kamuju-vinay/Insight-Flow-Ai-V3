# =============================================================================
# async_engine.py - Async Crawl Engine (connection pooling, caching, retry/backoff)
# Ported from notebook Cell 8
# =============================================================================
import asyncio
import logging
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup

from backend.config import (
    REQUEST_HEADERS, REQUEST_TIMEOUT_SECONDS, MAX_CONCURRENT_REQUESTS,
    MAX_RETRIES, RETRY_BACKOFF_BASE, MAX_PAGES_TO_INSPECT,
)
from backend.classifier import classify_article
from backend.date_extractor import extract_date
from backend.content import (
    extract_title, extract_content, summarize,
    canonical_url, content_hash, simhash64,
)

log = logging.getLogger(__name__)

_URL_CACHE: dict = {}  # url -> (html, headers) ; avoids duplicate requests within a run
_URL_CACHE_MAX_SIZE = 2000  # safety cap so a missed clear_url_cache() call can't OOM the process


def clear_url_cache():
    """Clear the in-memory URL cache between runs."""
    global _URL_CACHE
    _URL_CACHE = {}


def _cache_set(url, value):
    """Store into _URL_CACHE, evicting the oldest entry if over the size cap.
    Safety net on top of clear_url_cache() being called after every crawl
    run (see crawler.py) — without it, a missed clear lets full page HTML
    accumulate in memory indefinitely and can OOM the Railway container.
    """
    if len(_URL_CACHE) >= _URL_CACHE_MAX_SIZE:
        _URL_CACHE.pop(next(iter(_URL_CACHE)))
    _URL_CACHE[url] = value


async def _fetch_with_retry(session, semaphore, url):
    """Fetch one URL with bounded concurrency + exponential backoff retries.
    Returns (html_text, headers_dict) or (None, None) on permanent failure."""
    if url in _URL_CACHE:
        return _URL_CACHE[url]

    async with semaphore:
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with session.get(
                    url, headers=REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if (
                            "text/html" not in content_type
                            and "application/xhtml" not in content_type
                        ):
                            _cache_set(url, (None, None))
                            return None, None
                        html = await resp.text(errors="ignore")
                        # Guard: some servers mislabel XML/RSS as text/html
                        body_start = html.lstrip()[:200].lower()
                        if (
                            body_start.startswith("<?xml")
                            or body_start.startswith("<urlset")
                            or body_start.startswith("<sitemapindex")
                            or body_start.startswith("<rss")
                        ):
                            _cache_set(url, (None, None))
                            return None, None
                        headers = dict(resp.headers)
                        _cache_set(url, (html, headers))
                        return html, headers
                    elif resp.status in (429, 503):
                        await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                        continue
                    else:
                        _cache_set(url, (None, None))
                        return None, None
            except (asyncio.TimeoutError, aiohttp.ClientError):
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
            except Exception:
                _cache_set(url, (None, None))
                return None, None

    _cache_set(url, (None, None))
    return None, None


async def _process_one(session, semaphore, url, date_cutoff, reference_now):
    """Full per-URL pipeline: fetch -> classify -> extract title/date/content -> summarize."""
    html, headers = await _fetch_with_retry(session, semaphore, url)
    if not html:
        return None

    try:
        soup = BeautifulSoup(html, "lxml")
        is_article, details = classify_article(html, soup, url)
        if not is_article:
            return None

        published_date = extract_date(soup, url, headers, reference_now=reference_now)

        # Final date-window filter (sitemap <lastmod> was only a fast pre-filter)
        if date_cutoff is not None and published_date is not None:
            pub_dt = datetime.strptime(published_date, "%Y-%m-%d")
            if pub_dt < date_cutoff:
                return None

        title = extract_title(soup)
        content = extract_content(html, url, soup)
        summary = summarize(content, mode="extractive")

        # Extract metadata
        author = ""
        author_tag = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", attrs={"property": "article:author"})
        if author_tag and author_tag.get("content"):
            author = author_tag["content"].strip()
            
        language = ""
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            language = html_tag["lang"].strip()
        else:
            lang_tag = soup.find("meta", attrs={"name": "language"}) or soup.find("meta", attrs={"http-equiv": "content-language"})
            if lang_tag and lang_tag.get("content"):
                language = lang_tag["content"].strip()
                
        meta_desc = ""
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"].strip()
            
        modified_date = None
        mod_tag = soup.find("meta", attrs={"property": "article:modified_time"}) or soup.find("meta", attrs={"property": "og:updated_time"})
        if mod_tag and mod_tag.get("content"):
            modified_date = mod_tag["content"].strip()[:10]

        return {
            "Published Date": published_date,
            "Title": title,
            "Summary": summary,
            "Content": content,
            "URL": url,
            "_canonical_url": canonical_url(soup, url),
            "_content_hash": content_hash(content),
            "_simhash": simhash64(content),
            "Author": author,
            "Language": language,
            "Description": meta_desc,
            "Modified Date": modified_date,
        }
    except Exception as e:
        log.debug("Error on %s: %s", url, e)
        return None


async def run_async_pipeline(
    urls, date_cutoff=None, reference_now=None, max_pages=MAX_PAGES_TO_INSPECT
):
    """Downloads+classifies+extracts every URL concurrently with connection pooling."""
    reference_now = reference_now or datetime.now()
    urls = urls[:max_pages]

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _process_one(session, semaphore, url, date_cutoff, reference_now)
            for url in urls
        ]
        results = []
        for coro in asyncio.as_completed(tasks):
            record = await coro
            if record:
                results.append(record)
        return results


def crawl_articles(urls, date_cutoff=None, reference_now=None):
    """Synchronous wrapper -- safe to call from a background thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            run_async_pipeline(urls, date_cutoff=date_cutoff, reference_now=reference_now)
        )
    finally:
        loop.close()
