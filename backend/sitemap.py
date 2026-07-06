# =============================================================================
# sitemap.py - XML Sitemap Discovery
# Ported from notebook Cell 3
# =============================================================================
import urllib.parse as urlparse
import logging
from io import BytesIO

import requests
import pandas as pd
from lxml import etree

from backend.config import (
    REQUEST_HEADERS, REQUEST_TIMEOUT_SECONDS, COMMON_SITEMAP_PATHS
)

log = logging.getLogger(__name__)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class XMLPipelineStopped(Exception):
    """Raised when no usable XML sitemap can be found. Deliberate controlled stop."""
    pass


def _get(url, timeout=REQUEST_TIMEOUT_SECONDS):
    return requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)


def get_sitemaps_from_robots(base_url):
    """Extract 'Sitemap:' directives from robots.txt. Returns [] on any failure."""
    parsed = urlparse.urlsplit(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    sitemaps = []
    try:
        resp = _get(robots_url)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url:
                        sitemaps.append(sm_url)
    except Exception as e:
        log.warning("robots.txt lookup failed (%s); continuing with common paths.", e)
    return sitemaps


def download_xml(url):
    """Fetch a URL and parse it as XML. Raises on any failure (caller handles)."""
    resp = _get(url)
    resp.raise_for_status()
    if not resp.content:
        raise ValueError("empty response body")
    return etree.parse(BytesIO(resp.content))


def find_root_sitemap(base_url):
    """Try robots.txt directives first, then common well-known paths.
    Returns the first URL that parses as valid XML, else (None, None)."""
    parsed = urlparse.urlsplit(base_url)
    scheme_host = f"{parsed.scheme}://{parsed.netloc}"

    candidates = list(get_sitemaps_from_robots(base_url))
    candidates += [scheme_host + p for p in COMMON_SITEMAP_PATHS]

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            tree = download_xml(url)
            root_tag = etree.QName(tree.getroot()).localname
            if root_tag in ("sitemapindex", "urlset"):
                log.info("Found valid root sitemap: %s", url)
                return url, tree
        except Exception:
            continue
    return None, None


def build_child_sitemap_table(root_url, root_tree):
    """Given a root sitemap, return a DataFrame with columns: Child Sitemap URL | Last Modified.
    No classification is applied -- every child sitemap will be processed."""
    root_tag = etree.QName(root_tree.getroot()).localname
    rows = []

    if root_tag == "urlset":
        rows.append({"Child Sitemap URL": root_url, "Last Modified": ""})
        return pd.DataFrame(rows), {root_url: root_tree}

    trees_cache = {}
    for sitemap_el in root_tree.xpath("//sm:sitemap", namespaces=SITEMAP_NS):
        loc_el = sitemap_el.find("sm:loc", SITEMAP_NS)
        lastmod_el = sitemap_el.find("sm:lastmod", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else ""
        rows.append({"Child Sitemap URL": loc, "Last Modified": lastmod})

    df = pd.DataFrame(rows)
    if not df.empty:
        df.sort_values(by="Last Modified", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
    return df, trees_cache


def discover_and_classify_sitemaps(website_url):
    """Top-level entry point. Returns (child_sitemap_df, root_url) on success.
    Raises XMLPipelineStopped on failure."""
    log.info("Locating XML sitemap for %s ...", website_url)
    root_url, root_tree = find_root_sitemap(website_url)

    if root_url is None:
        log.warning(
            "XML Sitemap not found. Try: RSS Feed, Resource Pages, "
            "Pagination, BFS Crawl, JS Rendering."
        )
        raise XMLPipelineStopped("No usable XML sitemap found.")

    log.info("Building child-sitemap table (no classification) ...")
    child_df, _ = build_child_sitemap_table(root_url, root_tree)

    if child_df.empty:
        log.warning("XML sitemap contains no child sitemaps or URLs.")
        raise XMLPipelineStopped("Sitemap found but contained no usable entries.")

    return child_df, root_url
