# =============================================================================
# date_extractor.py - Date Extraction Module
# Ported from notebook Cell 5
# Priority: JSON-LD -> meta tags -> <time> -> labelled elements -> regex -> URL -> HTTP header
# =============================================================================
import re
import json as _json
import logging
from datetime import datetime

from dateutil import parser as dateparser

from backend.config import (
    RELATIVE_DATE_PATTERNS, JSONLD_DATE_KEYS,
    DATE_META_PROPERTY_NAMES, DATE_META_NAME_NAMES,
    DATE_LABEL_PHRASES, DATE_REGEX_PATTERNS,
)

log = logging.getLogger(__name__)
URL_DATE_PATTERN = re.compile(r"(20\d{2})[/\-_](\d{1,2})[/\-_](\d{1,2})")


def get_jsonld_blocks(soup):
    """Return every parsed JSON-LD dict on the page (flattening @graph and arrays)."""
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            data = _json.loads(raw)
            if isinstance(data, list):
                blocks.extend(data)
            elif isinstance(data, dict):
                if "@graph" in data and isinstance(data["@graph"], list):
                    blocks.extend(data["@graph"])
                else:
                    blocks.append(data)
        except Exception:
            continue
    return blocks


def normalize_date(raw_value, reference_now=None):
    """Parse an arbitrary date-like string/relative phrase and normalize to YYYY-MM-DD, or None."""
    if not raw_value:
        return None
    raw_value = str(raw_value).strip()
    reference_now = reference_now or datetime.now()

    # 1. Relative dates ("2 days ago", "yesterday", "just now" ...)
    for pattern, delta_fn in RELATIVE_DATE_PATTERNS:
        m = pattern.search(raw_value)
        if m:
            try:
                dt = reference_now - delta_fn(m)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass

    # 2. Absolute dates via dateutil (fuzzy, tolerates surrounding label text)
    try:
        dt = dateparser.parse(raw_value, fuzzy=True, default=datetime(1900, 1, 1))
        if dt.year < 1995 or dt.year > reference_now.year + 1:
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _find_regex_date_substring(text):
    """Scan text against every pattern in DATE_REGEX_PATTERNS; return first match string."""
    for pattern in DATE_REGEX_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def extract_date(soup, url, response_headers=None, jsonld_blocks=None, reference_now=None):
    """Try each date source in priority order; return first normalized YYYY-MM-DD, or None."""
    reference_now = reference_now or datetime.now()
    if jsonld_blocks is None:
        jsonld_blocks = get_jsonld_blocks(soup)

    # 1. JSON-LD (datePublished / dateCreated / uploadDate / dateModified)
    for key in JSONLD_DATE_KEYS:
        for block in jsonld_blocks:
            if isinstance(block, dict) and block.get(key):
                d = normalize_date(block[key], reference_now)
                if d:
                    return d

    # 2. Meta tags -- property=""
    for prop in DATE_META_PROPERTY_NAMES:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            d = normalize_date(tag["content"], reference_now)
            if d:
                return d

    # 3. Meta tags -- name=""
    for name in DATE_META_NAME_NAMES:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            d = normalize_date(tag["content"], reference_now)
            if d:
                return d

    # 4. <time datetime="">
    tag = soup.find("time", attrs={"datetime": True})
    if tag and tag.get("datetime"):
        d = normalize_date(tag["datetime"], reference_now)
        if d:
            return d

    # 5. Labelled HTML elements ("Published on ...", "Updated ...")
    label_candidates = soup.find_all(
        ["time", "span", "div", "p", "strong", "small", "header", "article", "section"],
        limit=300,
    )
    for el in label_candidates:
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 120:
            continue
        lowered = text.lower()
        if any(phrase in lowered for phrase in DATE_LABEL_PHRASES):
            substring = _find_regex_date_substring(text) or text
            d = normalize_date(substring, reference_now)
            if d:
                return d

    # 6. Bare regex pattern match in top 3000 chars of page text
    header_text = soup.get_text(" ", strip=True)[:3000]
    substring = _find_regex_date_substring(header_text)
    if substring:
        d = normalize_date(substring, reference_now)
        if d:
            return d

    # 7. URL date pattern (e.g. /2026/07/03/)
    match = URL_DATE_PATTERN.search(url)
    if match:
        y, m, d_ = match.groups()
        d = normalize_date(f"{y}-{m}-{d_}", reference_now)
        if d:
            return d

    # 8. HTTP Last-Modified response header (weakest signal, last resort)
    if response_headers:
        last_mod = response_headers.get("Last-Modified")
        if last_mod:
            d = normalize_date(last_mod, reference_now)
            if d:
                return d

    return None
