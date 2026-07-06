# =============================================================================
# rss.py - RSS / Atom Feed Discovery, Parsing, and Enrichment
# Ported from notebook Cells 10, 11, 12
# =============================================================================
import re
import asyncio
import logging
from datetime import datetime
from io import BytesIO
import urllib.parse as urlparse

import requests
import aiohttp
from bs4 import BeautifulSoup
from lxml import etree

from backend.config import (
    REQUEST_HEADERS, REQUEST_TIMEOUT_SECONDS, MAX_CONCURRENT_REQUESTS,
    FEED_LINK_TYPES, COMMON_FEED_PATHS, RSS_NS_ATOM, ALLOW_FEED_ONLY_FALLBACK,
)
from backend.url_extractor import normalize_url
from backend.date_extractor import normalize_date, extract_date
from backend.classifier import classify_article
from backend.content import (
    extract_title, extract_content, summarize,
    canonical_url, content_hash, simhash64,
)
from backend.async_engine import _fetch_with_retry

log = logging.getLogger(__name__)


# --- Feed discovery (Cell 10) ------------------------------------------------

def discover_feed_urls(website_url):
    """Find candidate RSS/Atom feed URLs.
    Homepage <link rel=alternate> hits come first (most reliable),
    then common well-known paths."""
    parsed = urlparse.urlsplit(website_url)
    scheme_host = f"{parsed.scheme}://{parsed.netloc}"
    candidates = []

    try:
        resp = requests.get(website_url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            for link in soup.find_all("link", attrs={"rel": "alternate"}):
                link_type = (link.get("type") or "").lower()
                href = link.get("href")
                if href and link_type in FEED_LINK_TYPES:
                    candidates.append(urlparse.urljoin(website_url, href))
    except Exception as e:
        log.warning("Could not inspect homepage <link> tags: %s", e)

    candidates += [scheme_host + p for p in COMMON_FEED_PATHS]

    seen = set()
    deduped = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _looks_like_feed(tree):
    """True if the parsed XML root is an RSS <rss> or an Atom <feed>."""
    try:
        root_tag = etree.QName(tree.getroot()).localname.lower()
        return root_tag in ("rss", "feed")
    except Exception:
        return False


def find_working_feed(website_url):
    """Try every candidate feed URL; return first genuine RSS/Atom doc.
    Returns (None, None) if nothing works."""
    for feed_url in discover_feed_urls(website_url):
        try:
            resp = requests.get(feed_url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code != 200 or not resp.content:
                continue
            tree = etree.parse(BytesIO(resp.content))
            if _looks_like_feed(tree):
                log.info("Found working feed: %s", feed_url)
                return feed_url, tree
        except Exception:
            continue
    return None, None


# --- Feed item parsing (Cell 11) ---------------------------------------------

def _strip_html(text):
    """Strip HTML tags from RSS <description> / Atom <summary> text."""
    if not text:
        return ""
    try:
        return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", text).strip()


def parse_rss_items(tree):
    """Extract items from an RSS 2.0 <rss><channel><item> document."""
    items = []
    for item_el in tree.xpath("//item"):
        link_el = item_el.find("link")
        title_el = item_el.find("title")
        pubdate_el = item_el.find("pubDate")
        desc_el = item_el.find("description")

        url = (link_el.text or "").strip() if link_el is not None and link_el.text else None
        if not url:
            continue

        items.append({
            "url": normalize_url(url),
            "title": (title_el.text or "").strip() if title_el is not None and title_el.text else "",
            "published_raw": (pubdate_el.text or "").strip() if pubdate_el is not None and pubdate_el.text else "",
            "summary_raw": _strip_html(desc_el.text) if desc_el is not None else "",
        })
    return items


def parse_atom_items(tree):
    """Extract entries from an Atom <feed><entry> document.
    NOTE: uses explicit 'is not None' checks because lxml Elements are falsy
    when they have zero child elements, even if they have real text content."""
    items = []
    for entry_el in tree.xpath("//atom:entry", namespaces=RSS_NS_ATOM):
        title_el = entry_el.find("atom:title", RSS_NS_ATOM)

        published_el = entry_el.find("atom:published", RSS_NS_ATOM)
        if published_el is None:
            published_el = entry_el.find("atom:updated", RSS_NS_ATOM)

        summary_el = entry_el.find("atom:summary", RSS_NS_ATOM)
        if summary_el is None:
            summary_el = entry_el.find("atom:content", RSS_NS_ATOM)

        url = None
        for link_el in entry_el.findall("atom:link", RSS_NS_ATOM):
            rel = link_el.get("rel", "alternate")
            if rel == "alternate" and link_el.get("href"):
                url = link_el.get("href")
                break
        if not url:
            link_el = entry_el.find("atom:link", RSS_NS_ATOM)
            if link_el is not None and link_el.get("href"):
                url = link_el.get("href")
        if not url:
            continue

        items.append({
            "url": normalize_url(url),
            "title": (title_el.text or "").strip() if title_el is not None and title_el.text else "",
            "published_raw": (published_el.text or "").strip() if published_el is not None and published_el.text else "",
            "summary_raw": _strip_html(summary_el.text) if summary_el is not None else "",
        })
    return items


def parse_feed_items(tree):
    """Dispatch to the RSS or Atom parser based on the document root tag."""
    root_tag = etree.QName(tree.getroot()).localname.lower()
    if root_tag == "feed":
        return parse_atom_items(tree)
    return parse_rss_items(tree)


def collect_feed_candidates(feed_url, feed_tree, date_cutoff=None):
    """Parse a feed into candidate records, normalizing dates and applying date-window pre-filter."""
    raw_items = parse_feed_items(feed_tree)
    log.info("%s -> %d item(s) in feed", feed_url, len(raw_items))

    candidates = []
    for item in raw_items:
        published = normalize_date(item["published_raw"]) if item["published_raw"] else None
        if date_cutoff is not None and published is not None:
            published_dt = datetime.strptime(published, "%Y-%m-%d")
            if published_dt < date_cutoff:
                continue
        candidates.append({
            "url": item["url"],
            "title": item["title"],
            "feed_published": published,
            "feed_summary": item["summary_raw"],
        })
    return candidates


# --- Feed item enrichment (Cell 12) ------------------------------------------

async def _enrich_one_feed_item(session, semaphore, candidate, date_cutoff, reference_now):
    """Download, classify, and extract one feed item. Reuses async_engine fetch."""
    url = candidate["url"]
    html, headers = await _fetch_with_retry(session, semaphore, url)

    if not html:
        if ALLOW_FEED_ONLY_FALLBACK and candidate["title"]:
            return {
                "Published Date": candidate["feed_published"],
                "Title": candidate["title"],
                "Summary": candidate["feed_summary"][:1500],
                "URL": url,
                "_canonical_url": normalize_url(url),
                "_content_hash": content_hash(candidate["feed_summary"]),
                "_simhash": simhash64(candidate["feed_summary"]),
            }
        return None

    try:
        soup = BeautifulSoup(html, "lxml")
        is_article, details = classify_article(html, soup, url)
        if not is_article:
            return None

        page_date = extract_date(soup, url, headers, reference_now=reference_now)
        published_date = page_date or candidate["feed_published"]

        if date_cutoff is not None and published_date is not None:
            pub_dt = datetime.strptime(published_date, "%Y-%m-%d")
            if pub_dt < date_cutoff:
                return None

        page_title = extract_title(soup)
        title = (
            page_title
            if page_title and page_title != "Untitled"
            else (candidate["title"] or "Untitled")
        )

        content = extract_content(html, url, soup)
        if len(content.split()) < 30 and candidate["feed_summary"]:
            content = candidate["feed_summary"]
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
        log.debug("feed-enrich error on %s: %s", url, e)
        return None


async def run_async_feed_pipeline(candidates, date_cutoff=None, reference_now=None):
    reference_now = reference_now or datetime.now()
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _enrich_one_feed_item(session, semaphore, c, date_cutoff, reference_now)
            for c in candidates
        ]
        results = []
        for coro in asyncio.as_completed(tasks):
            record = await coro
            if record:
                results.append(record)
        return results


def enrich_feed_candidates(candidates, date_cutoff=None, reference_now=None):
    """Synchronous wrapper -- safe to call from a background thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            run_async_feed_pipeline(
                candidates, date_cutoff=date_cutoff, reference_now=reference_now
            )
        )
    finally:
        loop.close()
