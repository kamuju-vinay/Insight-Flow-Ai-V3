# =============================================================================
# url_extractor.py - Article URL Extraction from Every Child Sitemap
# Ported from notebook Cell 4
# =============================================================================
import urllib.parse as urlparse
import logging
from datetime import datetime
from io import BytesIO

import requests
from lxml import etree

from backend.config import (
    NON_PAGE_EXTENSIONS, REQUEST_HEADERS, REQUEST_TIMEOUT_SECONDS
)

log = logging.getLogger(__name__)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def normalize_url(url):
    """Strip fragments; keep query strings; drop trailing slash except for bare domains."""
    try:
        parsed = urlparse.urlsplit(url)
        cleaned = parsed._replace(fragment="")
        normalized = urlparse.urlunsplit(cleaned)
        return normalized.rstrip("/") if normalized.count("/") > 2 else normalized
    except Exception:
        return url


def is_downloadable_page(url):
    """True unless the URL is obviously a non-HTML resource (image/video/pdf/data/feed)."""
    lowered = url.lower().split("?")[0]
    return not lowered.endswith(NON_PAGE_EXTENSIONS)


def parse_lastmod(value):
    """Best-effort ISO8601 parse of a sitemap <lastmod> value. Returns naive datetime or None."""
    if not value:
        return None
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def _download_xml(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    if not resp.content:
        raise ValueError("empty response body")
    return etree.parse(BytesIO(resp.content))


def extract_urls_from_sitemap(sitemap_url, date_cutoff=None):
    """Download one child sitemap and return every <url><loc> entry as a list of dicts."""
    rows = []
    try:
        tree = _download_xml(sitemap_url)
    except Exception as e:
        log.warning("Could not download %s: %s", sitemap_url, e)
        return rows

    for url_el in tree.xpath("//sm:url", namespaces=SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = normalize_url(loc_el.text.strip())
        if not is_downloadable_page(loc):
            continue

        lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
        lastmod_raw = (
            lastmod_el.text.strip()
            if lastmod_el is not None and lastmod_el.text
            else ""
        )
        lastmod_dt = parse_lastmod(lastmod_raw)

        # Fast pre-filter: skip if clearly before the date window
        if date_cutoff is not None and lastmod_dt is not None and lastmod_dt < date_cutoff:
            continue

        rows.append({
            "url": loc,
            "lastmod_raw": lastmod_raw,
            "lastmod_dt": lastmod_dt,
            "source_sitemap": sitemap_url,
        })

    return rows


def collect_candidate_urls(child_df, date_cutoff=None):
    """Process EVERY child sitemap found in the index, in order.
    No sitemap is skipped by filename. Returns a deduplicated list of URL dicts."""
    all_rows = []
    seen_urls = set()

    log.info("Processing all %d child sitemap(s) -- none skipped by filename ...", len(child_df))
    for _, row in child_df.iterrows():
        sm_url = row["Child Sitemap URL"]
        rows = extract_urls_from_sitemap(sm_url, date_cutoff=date_cutoff)
        new_count = 0
        for r in rows:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_rows.append(r)
                new_count += 1
        log.info("%s -> %d URL(s), %d new after de-dup", sm_url, len(rows), new_count)

    log.info("Total unique candidate URLs (pre-content-check): %d", len(all_rows))
    return all_rows
