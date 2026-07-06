# =============================================================================
# content.py - Title/Content Extraction, Deduplication, Summarization
# Ported from notebook Cell 7
# =============================================================================
import re
import hashlib
import difflib
import urllib.parse as urlparse
import logging
from collections import Counter
from bs4 import BeautifulSoup

from backend.config import (
    TITLE_SIMILARITY_THRESHOLD, SIMHASH_HAMMING_THRESHOLD,
    SUMMARY_MAX_WORDS, SUMMARY_MIN_SENTENCES, SUMMARY_MAX_SENTENCES,
)

log = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:
    trafilatura = None

NOISE_KEYWORDS = (
    "header", "footer", "nav", "navbar", "menu", "cookie", "consent", "banner",
    "sidebar", "advert", "ads", "ad-", "promo", "subscribe", "share", "social",
    "comment", "related", "breadcrumb",
)


# --- Title extraction --------------------------------------------------------

def extract_title(soup):
    """Priority: h1 -> og:title -> <title>."""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content", "").strip():
        return og_title["content"].strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        return title_tag.get_text(strip=True)
    return "Untitled"


# --- Content extraction ------------------------------------------------------

def _looks_like_noise(tag):
    attrs_text = " ".join([
        " ".join(tag.get("class", [])) if tag.has_attr("class") else "",
        tag.get("id", "") or "",
    ]).lower()
    return any(kw in attrs_text for kw in NOISE_KEYWORDS)


def extract_content_bs4_fallback(soup):
    soup = BeautifulSoup(str(soup), "lxml")
    for tag_name in ("header", "footer", "nav", "aside", "script", "style", "noscript", "form", "template", "iframe"):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(True):
        try:
            if _looks_like_noise(tag):
                tag.decompose()
        except Exception:
            continue
    paragraphs = soup.find_all("p")
    text = "\n".join(p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True))
    return text.strip()


def extract_content(html, url, soup):
    """Prefer trafilatura; fall back to boilerplate-stripped BeautifulSoup extraction."""
    text = None
    if trafilatura is not None:
        try:
            text = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=False, favor_precision=True,
            )
        except Exception:
            text = None
    if not text or len(text.split()) < 50:
        try:
            fallback_text = extract_content_bs4_fallback(soup)
            if fallback_text and len(fallback_text.split()) > len((text or "").split()):
                text = fallback_text
        except Exception:
            pass
    return _clean_residual_html(text or "").strip()


# Detects leftover markup (e.g. hidden <template>/AJAX "load more" blocks some
# sites embed for infinite-scroll post cards) that slipped through trafilatura
# or the bs4 fallback and ended up as literal text instead of being stripped.
_HTML_TAG_PATTERN = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>")


def _clean_residual_html(text):
    if not text:
        return text
    tag_hits = len(_HTML_TAG_PATTERN.findall(text))
    # A handful of stray "<a>"-style mentions in prose is normal; dozens of
    # real HTML tags (div/span/class=...) means a raw markup block leaked in.
    if tag_hits < 5:
        return text
    log.warning("extract_content: stripping %d residual HTML tags from extracted text", tag_hits)
    cleaned_soup = BeautifulSoup(text, "html.parser")
    for tag_name in ("script", "style", "template", "noscript"):
        for tag in cleaned_soup.find_all(tag_name):
            tag.decompose()
    cleaned = cleaned_soup.get_text(" ", strip=True)
    # Collapse the class-name/attribute soup that's left once tags are gone
    # (e.g. "col-sm-3 post-46892 post type-post status-publish...").
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


# --- Canonical URL + deduplication -------------------------------------------

def _slug(url):
    """Last non-empty path segment of a URL."""
    try:
        path = urlparse.urlsplit(url).path.strip("/")
        parts = [p for p in path.split("/") if p]
        return parts[-1].lower() if parts else ""
    except Exception:
        return ""


def _normalize_url(url):
    """Strip fragments; drop trailing slash except for bare domains."""
    try:
        parsed = urlparse.urlsplit(url)
        cleaned = parsed._replace(fragment="")
        normalized = urlparse.urlunsplit(cleaned)
        return normalized.rstrip("/") if normalized.count("/") > 2 else normalized
    except Exception:
        return url


def canonical_url(soup, fallback_url):
    """Prefer <link rel=canonical> only when it clearly still points at the SAME article."""
    link = soup.find("link", attrs={"rel": "canonical"})
    if link and link.get("href"):
        candidate = _normalize_url(link["href"])
        fetched_slug = _slug(fallback_url)
        canonical_slug = _slug(candidate)
        if fetched_slug and canonical_slug:
            same_article = (
                fetched_slug == canonical_slug
                or fetched_slug in canonical_slug
                or canonical_slug in fetched_slug
            )
            if same_article:
                return candidate
        return _normalize_url(fallback_url)
    return _normalize_url(fallback_url)


def content_hash(text):
    """Exact-duplicate detector: MD5 of normalized whitespace content."""
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _shingles(text, k=4):
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def simhash64(text):
    """Dependency-free 64-bit SimHash over word 4-shingles."""
    shingles = _shingles(text)
    if not shingles:
        return 0
    v = [0] * 64
    for sh in shingles:
        h = int(hashlib.md5(sh.encode("utf-8")).hexdigest(), 16)
        for bit in range(64):
            v[bit] += 1 if (h >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(64):
        if v[bit] > 0:
            fingerprint |= (1 << bit)
    return fingerprint


def hamming_distance(a, b):
    return bin(a ^ b).count("1")


def title_similarity(title_a, title_b):
    if not title_a or not title_b:
        return 0.0
    return difflib.SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio()


def deduplicate_records(records):
    """Remove duplicates using: canonical URL, content hash, SimHash, title similarity."""
    seen_urls = set()
    seen_hashes = set()
    kept = []
    kept_simhashes = []
    kept_titles = []

    for rec in records:
        url = rec.get("_canonical_url") or rec["URL"]
        if url in seen_urls:
            continue

        c_hash = rec.get("_content_hash")
        if c_hash and c_hash in seen_hashes:
            continue

        s_hash = rec.get("_simhash")
        is_near_dup = False
        if s_hash:
            for existing_hash, _ in kept_simhashes:
                if hamming_distance(s_hash, existing_hash) <= SIMHASH_HAMMING_THRESHOLD:
                    is_near_dup = True
                    break
        if is_near_dup:
            continue

        title = rec.get("Title", "")
        for existing_title, _ in kept_titles:
            if title_similarity(title, existing_title) >= TITLE_SIMILARITY_THRESHOLD:
                is_near_dup = True
                break
        if is_near_dup:
            continue

        seen_urls.add(url)
        if c_hash:
            seen_hashes.add(c_hash)
        if s_hash:
            kept_simhashes.append((s_hash, len(kept)))
        kept_titles.append((title, len(kept)))
        kept.append(rec)

    return kept


# --- Summarization -----------------------------------------------------------

SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
STOPWORDS = set((
    "the a an and or but if while is are was were be been being of to in on at by for with "
    "about against between into through during before after above below from up down out off "
    "over under again further then once here there all any both each few more most other some "
    "such no nor not only own same so than too very s t can will just don should now this that "
    "these those it its as also"
).split())


def _extractive_rank(content, max_sentences):
    sentences = [
        s.strip()
        for s in SENTENCE_SPLIT_PATTERN.split(content.replace("\n", " "))
        if s.strip()
    ]
    if not sentences:
        return []

    word_freq = Counter()
    for sent in sentences:
        for w in re.findall(r"[a-zA-Z']+", sent.lower()):
            if w not in STOPWORDS and len(w) > 2:
                word_freq[w] += 1

    if not word_freq:
        return sentences[:max_sentences]

    max_freq = max(word_freq.values())
    scored = []
    for idx, sent in enumerate(sentences):
        words = re.findall(r"[a-zA-Z']+", sent.lower())
        if len(words) < 4:
            continue
        score = sum(word_freq.get(w, 0) / max_freq for w in words) / len(words)
        position_bonus = 1.0 if idx < 3 else 0.85
        scored.append((score * position_bonus, idx, sent))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = sorted(scored[:max_sentences], key=lambda x: x[1])
    return [s for _, _, s in top]


def summarize_extractive(
    content,
    max_words=SUMMARY_MAX_WORDS,
    min_sentences=SUMMARY_MIN_SENTENCES,
    max_sentences=SUMMARY_MAX_SENTENCES,
):
    if not content:
        return ""
    chosen = _extractive_rank(content, max_sentences)
    if len(chosen) < min_sentences:
        sentences = [
            s.strip()
            for s in SENTENCE_SPLIT_PATTERN.split(content.replace("\n", " "))
            if s.strip()
        ]
        chosen = sentences[:max_sentences]

    summary = " ".join(chosen).strip()
    words = summary.split()
    if len(words) > max_words:
        summary = " ".join(words[:max_words]).rstrip(",.;:") + "..."
    return summary


def summarize(content, mode="extractive"):
    """Pluggable summarizer. mode='extractive' (default) or 'llm' (future)."""
    if mode == "extractive":
        return summarize_extractive(content)
    raise NotImplementedError(f"Summarizer mode '{mode}' not implemented yet.")
