# =============================================================================
# crawler.py — 3-Case Article Discovery Engine
# Ported faithfully from insight_flow_main_logic.ipynb & ESG_AGENT (2).ipynb
#
# Case 1: Sitemap/feed found AND every entry already has a reliable date
#         → Trust XML date directly; keep every URL in date range, no AI filter.
# Case 2: Sitemap/feed found BUT dates are missing/unreliable in XML
#         → Visit every URL, extract publish date from the page itself, filter by range.
# Case 3: No sitemap/feed found at all (or BFS Direct mode selected)
#         → BFS crawl from seed URL, extract date from every fetched page, filter.
# =============================================================================
import re
import json
import asyncio
import logging
import threading
import warnings
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, urldefrag

import aiohttp
from bs4 import BeautifulSoup
from lxml import etree
from dateutil import parser as dateparser

from backend.config import MAX_PAGES_TO_INSPECT
from backend.classifier import calculate_relevance_score
from backend.content import summarize
from backend.date_extractor import normalize_date
from backend.db import (
    save_article, save_log, get_plan, save_plan,
    get_articles, is_seen_url, add_seen_url, get_settings,
)

log = logging.getLogger(__name__)

# ── Thread-safety ─────────────────────────────────────────────────────────────
_running_plans: set = set()
_running_lock = threading.Lock()

# ── HTTP / async config ────────────────────────────────────────────────────────
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InsightFlowBot/2.0; +https://example.com/bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = 10
MAX_CONCURRENT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

# ── Sitemap discovery paths ────────────────────────────────────────────────────
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap/sitemap.xml", "/wp-sitemap.xml", "/sitemapindex.xml",
    "/news-sitemap.xml", "/sitemap1.xml",
]
COMMON_FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml"]
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
STRUCTURED_DATE_COVERAGE_THRESHOLD = 0.9

# ── URL filtering (link discovery only) ───────────────────────────────────────
SKIP_EXTENSIONS = (
    ".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp3", ".mp4", ".mov", ".avi", ".xml", ".json", ".css", ".js", ".woff",
    ".woff2", ".ttf", ".eot",
)
SKIP_URL_KEYWORDS = (
    "/login", "/logout", "/signin", "/signup", "/register", "/privacy",
    "/terms", "/contact", "/search", "/cart", "/checkout", "/wp-json",
    "/wp-admin", "/wp-login", "mailto:", "tel:", "javascript:",
    "/tag/", "/tags/", "/category/", "/categories/", "/author/",
    "/page/", "?s=", "/events", "/videos",
)

# ── Date extraction config ─────────────────────────────────────────────────────
DATE_REGEX_PATTEREN_STRINGS = [
    re.compile(r"\b\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}\b"),
    re.compile(r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"),
    re.compile(r"\b\d{1,2}[\-\s][A-Za-z]{3,9}[\-\s]\d{4}\b"),
    re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\\d{2})?\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\s+\d{1,2}:\\d{2}\s*(?:AM|PM|am|pm)?\b"),
    re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{1,2}:\\d{2}\s*(?:IST|UTC|GMT|EST|PST)?\b"),
]
RELATIVE_DATE_PATTERNS = [
    (re.compile(r"\bjust now\b", re.I), lambda m: timedelta(minutes=0)),
    (re.compile(r"\btoday\b", re.I), lambda m: timedelta(days=0)),
    (re.compile(r"\byesterday\b", re.I), lambda m: timedelta(days=1)),
    (re.compile(r"(\d+)\s+minutes?\s+ago", re.I), lambda m: timedelta(minutes=int(m.group(1)))),
    (re.compile(r"(\d+)\s+hours?\s+ago", re.I), lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"(\d+)\s+days?\s+ago", re.I), lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s+weeks?\s+ago", re.I), lambda m: timedelta(weeks=int(m.group(1)))),
    (re.compile(r"(\d+)\s+months?\s+ago", re.I), lambda m: timedelta(days=int(m.group(1)) * 30)),
    (re.compile(r"(\d+)\s+years?\s+ago", re.I), lambda m: timedelta(days=int(m.group(1)) * 365)),
]
DATE_META_PROPERTY_NAMES = ["article:published_time", "article:modified_time", "og:updated_time"]
DATE_META_NAME_NAMES = [
    "publish-date", "publish_date", "publishdate", "pubdate", "date",
    "dc.date", "dc.date.created", "dc.date.modified", "dc.Date",
    "sailthru.date", "parsely-pub-date", "datePublished", "dateModified",
]
JSONLD_DATE_KEYS = ["datePublished", "dateCreated", "uploadDate", "dateModified"]
URL_DATE_PATTERN = re.compile(r"(20\d{2})[/\-_](\d{1,2})[/\-_](\d{1,2})")
DATE_LABEL_PHRASES = (
    "published", "published on", "publish date", "published date",
    "publication date", "date published", "posted", "posted on",
    "release date", "released", "created", "created on",
    "first published", "originally published", "updated", "last updated",
)
DATE_CLASS_HINTS = (
    "header-publish-date", "publish-date", "published-date", "post-date",
    "postdate", "entry-date", "article-date", "date-published", "pubdate",
    "date-display", "byline-date", "meta-date", "content-date",
)


# =============================================================================
# LOGGING HELPER
# =============================================================================
def _log(event: str, plan_name: str = "", log_type: str = "info"):
    try:
        save_log(event, plan_name, log_type)
    except Exception:
        pass


def update_crawl_progress(plan_id: str, step: str, progress: int, url_index: int = 0, completed_urls: list = None, failed_urls: list = None, is_active: bool = True):
    try:
        plan = get_plan(plan_id)
        if not plan:
            return
        existing_state = plan.get("crawlState") or {}
        comp = completed_urls if completed_urls is not None else existing_state.get("completedUrls", [])
        fail = failed_urls if failed_urls is not None else existing_state.get("failedUrls", [])
        
        crawl_state = {
            "urlIndex": url_index,
            "completedUrls": comp,
            "failedUrls": fail,
            "step": step,
            "progress": progress,
            "isActive": is_active
        }
        plan["crawlState"] = crawl_state
        if is_active:
            plan["stage"] = "crawling"
            plan["status"] = "running"
        else:
            plan["stage"] = "done"
            plan["status"] = "running"
        save_plan(plan)
    except Exception as e:
        log.error("Failed to update crawl progress: %s", e)



# =============================================================================
# DATE CUTOFF HELPERS
# =============================================================================
def _date_cutoff_for_plan(plan: dict):
    fetch_period = plan.get("fetchPeriod", "week")
    fetch_days = plan.get("fetchPeriodDays", 7)
    
    try:
        fetch_days = int(fetch_days)
    except (TypeError, ValueError):
        fetch_days = 7

    now = datetime.now()
    
    if fetch_period == "day":
        return now - timedelta(days=1)
    elif fetch_period == "week":
        return now - timedelta(days=7)
    elif fetch_period == "month":
        return now - timedelta(days=30)
    elif fetch_period == "custom":
        return now - timedelta(days=fetch_days)
        
    date_range = plan.get("dateRange")
    if date_range:
        mapping = {
            "last7":  now - timedelta(days=7),
            "last15": now - timedelta(days=15),
            "last30": now - timedelta(days=30),
            "last90": now - timedelta(days=90),
        }
        if date_range == "custom":
            try:
                return datetime.strptime(plan.get("dateFrom", ""), "%Y-%m-%d")
            except Exception:
                pass
        return mapping.get(date_range, now - timedelta(days=30))
        
    return now - timedelta(days=30)


def _custom_end_for_plan(plan: dict):
    if plan.get("dateRange") == "custom" and plan.get("dateTo"):
        try:
            return datetime.strptime(plan["dateTo"], "%Y-%m-%d")
        except Exception:
            pass
    return None


def _to_date(dt: datetime):
    """Strip time portion for comparison."""
    return dt.date() if isinstance(dt, datetime) else dt


# =============================================================================
# ASYNC FETCH ENGINE
# =============================================================================
async def _fetch_text(session, url, sem, plan_name=None):
    """GET url, return HTML text or None. Retries on 429/5xx."""
    for attempt in range(MAX_RETRIES):
        try:
            async with sem:
                async with session.get(
                    url, headers=REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ssl=False, allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        ctype = resp.headers.get("Content-Type", "")
                        if "text/html" in ctype or "application/xhtml" in ctype or ctype == "":
                            html = await resp.text(errors="ignore")
                            _log(f"Fetched: {url}", plan_name, "crawl")
                            return html
                        html = await resp.text(errors="ignore")  # try anyway
                        _log(f"Fetched: {url}", plan_name, "crawl")
                        return html
                    if resp.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                        continue
                    _log(f"Failed to fetch: {url} (status {resp.status})", plan_name, "crawl")
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == MAX_RETRIES - 1:
                _log(f"Failed to fetch: {url} (error: {e})", plan_name, "crawl")
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
        except Exception as e:
            _log(f"Failed to fetch: {url} (error: {e})", plan_name, "crawl")
            return None
    return None


async def _fetch_many_async(urls, plan_name=None):
    """Fetch all urls concurrently. Returns {url: html_or_None}."""
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, ssl=False)
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results = {}
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = {u: asyncio.create_task(_fetch_text(session, u, sem, plan_name)) for u in urls}
        for u, t in tasks.items():
            results[u] = await t
    return results


def fetch_many(urls, plan_name=None):
    """Synchronous wrapper — runs the async fetch in a new event loop."""
    global REQUEST_TIMEOUT, MAX_CONCURRENT, MAX_RETRIES
    try:
        cfg = get_settings()
        timeout_val = cfg.get("timeout")
        if timeout_val is not None:
            REQUEST_TIMEOUT = int(timeout_val)
        else:
            REQUEST_TIMEOUT = 10
            
        workers = cfg.get("concurrent_workers")
        if workers is not None:
            MAX_CONCURRENT = int(workers)
            
        retries = cfg.get("retry_count")
        if retries is not None:
            MAX_RETRIES = int(retries)
            
        ua = cfg.get("user_agent")
        if ua:
            REQUEST_HEADERS["User-Agent"] = ua
    except Exception:
        REQUEST_TIMEOUT = 10

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch_many_async(urls, plan_name))
    finally:
        loop.close()


# =============================================================================
# NEWSPAPER3K ARTICLE EXTRACTION & FALLBACKS
# =============================================================================
def extract_article_with_newspaper(url, html=None):
    """Extract article text, title, date using newspaper3k with BS4 fallback."""
    try:
        from newspaper import Article
        article = Article(url)
        if html:
            article.set_html(html)
            article.parse()
        else:
            article.download()
            article.parse()

        pub_date = article.publish_date
        date_str = None
        if pub_date:
            if isinstance(pub_date, datetime):
                date_str = pub_date.strftime("%Y-%m-%d")
            else:
                date_str = str(pub_date)[:10]

        title = article.title or ""
        text = article.text or ""

        if not title and html:
            title = extract_title_from_html(html)
        if not date_str and html:
            date_str = extract_date_from_html(html, url)

        return {
            "title": title,
            "text": text,
            "date": date_str,
            "url": url
        }
    except Exception as e:
        log.error("Newspaper extraction failed for %s: %s", url, e)
        if html:
            return {
                "title": extract_title_from_html(html),
                "text": html,
                "date": extract_date_from_html(html, url),
                "url": url
            }
        return None


# =============================================================================
# UNIFIED LLM API CALLER
# =============================================================================
def call_llm(system_prompt: str, user_prompt: str, model_override: str = None) -> str:
    """Calls the active LLM provider configured in the app settings."""
    import httpx
    cfg = get_settings()
    provider = cfg.get("ai_provider", "gemini")

    # Resolve API Key
    api_key = ""
    if provider == "gemini":
        api_key = cfg.get("gemini_api_key") or cfg.get("gemini_api_key_primary")
    elif provider == "openai":
        api_key = cfg.get("openai_api_key") or cfg.get("openai_api_key_primary")
    elif provider == "groq":
        api_key = cfg.get("groq_api_key") or cfg.get("groq_api_key_primary")
    elif provider == "huggingface":
        api_key = cfg.get("huggingface_api_key") or cfg.get("huggingface_api_key_primary")
    elif provider == "claude" or provider == "anthropic":
        api_key = cfg.get("anthropic_api_key") or cfg.get("anthropic_api_key_primary")
        provider = "claude"

    if not api_key:
        # Fallback to any non-empty key
        for p in ["gemini", "openai", "groq", "huggingface", "anthropic"]:
            key = cfg.get(f"{p}_api_key") or cfg.get(f"{p}_api_key_primary")
            if key:
                provider = "claude" if p == "anthropic" else p
                api_key = key
                break

    if not api_key:
        log.warning("No API keys found in settings. Skipping AI call.")
        return ""

    try:
        with httpx.Client(timeout=60.0) as client:
            if provider == "gemini":
                model = model_override or "gemini-2.0-flash"
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                resp = client.post(url, json={
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}]
                })
                data = resp.json()
                if "candidates" in data and len(data["candidates"]) > 0:
                    return data["candidates"][0]["content"]["parts"][0].get("text", "").strip()

            elif provider == "openai":
                model = model_override or "gpt-4o-mini"
                url = "https://api.openai.com/v1/chat/completions"
                resp = client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                })
                data = resp.json()
                return data["choices"][0]["message"].get("content", "").strip()

            elif provider == "groq":
                model = model_override or "llama-3.3-70b-versatile"
                url = "https://api.groq.com/openai/v1/chat/completions"
                resp = client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                })
                data = resp.json()
                return data["choices"][0]["message"].get("content", "").strip()

            elif provider == "huggingface":
                model = model_override or cfg.get("huggingface_model") or "meta-llama/Llama-3.2-3B-Instruct"
                url = "https://router.huggingface.co/v1/chat/completions"
                resp = client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                })
                data = resp.json()
                return data["choices"][0]["message"].get("content", "").strip()

            elif provider == "claude":
                model = model_override or cfg.get("anthropic_model") or "claude-3-5-sonnet-20241022"
                url = "https://api.anthropic.com/v1/messages"
                resp = client.post(url, headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                }, json={
                    "model": model,
                    "max_tokens": 1000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}]
                })
                data = resp.json()
                return data["content"][0].get("text", "").strip()

    except Exception as e:
        log.error("LLM call failed: %s", e)
    return ""


# =============================================================================
# ESG ENVIRONMENT CLASSIFICATION & SUMMARIZATION (ESG_AGENT (2).ipynb)
# =============================================================================
def is_environment_related(title: str, text: str) -> bool:
    system_prompt = "You are an ESG analyst."
    user_prompt = f"""Determine whether this article is related to:

- ESG
- Environment
- Sustainability
- Climate
- Renewable Energy
- Carbon Reduction
- Net Zero
- Biodiversity

Return ONLY:

YES

or

NO

Title:
{title}

Article:
{text[:5000]}"""

    res = call_llm(system_prompt, user_prompt)
    if not res:
        # Fallback to True so we don't drop articles if no API key is available
        log.info("No API key available for classification, keeping article: '%s'", title)
        return True
    log.info("AI ESG classification response for '%s': %s", title, res)
    return "YES" in res.upper()


def generate_summary_llm(title: str, text: str) -> str:
    system_prompt = "You are an ESG analyst."
    user_prompt = f"""Summarize the article.

Provide:

1. Main announcement
2. ESG impact
3. Important metrics

Maximum 150 words.

Title:
{title}

Article:
{text[:8000]}"""

    res = call_llm(system_prompt, user_prompt)
    if not res:
        return summarize(text)  # fallback to extractive summary
    return res


# =============================================================================
# DATE & TITLE EXTRACTION (Page-level — Cases 2 & 3)
# =============================================================================
def _get_jsonld_blocks(soup):
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            data = json.loads(raw)
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


def _find_regex_date(text):
    for pattern in DATE_REGEX_PATTEREN_STRINGS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def extract_date_from_html(html, url, reference_now=None):
    """Priority: JSON-LD → meta → <time> → CSS hints → label scan → regex → URL pattern."""
    reference_now = reference_now or datetime.now()
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for block in _get_jsonld_blocks(soup):
        if not isinstance(block, dict):
            continue
        for key in JSONLD_DATE_KEYS:
            if block.get(key):
                d = normalize_date(block[key], reference_now)
                if d:
                    return d

    for prop in DATE_META_PROPERTY_NAMES:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            d = normalize_date(tag["content"], reference_now)
            if d:
                return d

    for name in DATE_META_NAME_NAMES:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            d = normalize_date(tag["content"], reference_now)
            if d:
                return d

    time_tag = soup.find("time")
    if time_tag:
        raw = time_tag.get("datetime") or time_tag.get_text(strip=True)
        d = normalize_date(raw, reference_now)
        if d:
            return d

    for hint in DATE_CLASS_HINTS:
        hint_re = re.compile(re.escape(hint), re.I)
        el = soup.find(attrs={"class": hint_re}) or soup.find(attrs={"id": hint_re})
        if el is None:
            continue
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        sub = _find_regex_date(txt)
        d = normalize_date(sub, reference_now) if sub else normalize_date(txt, reference_now)
        if d:
            return d

    for el in soup.find_all(["span", "div", "p"], limit=400):
        txt = el.get_text(" ", strip=True)
        if not txt or len(txt) > 120:
            continue
        low = txt.lower()
        if any(phrase in low for phrase in DATE_LABEL_PHRASES):
            sub = _find_regex_date(txt)
            if sub:
                d = normalize_date(sub, reference_now)
                if d:
                    return d
            d = normalize_date(txt, reference_now)
            if d:
                return d

    body_text = soup.get_text(" ", strip=True)[:5000]
    sub = _find_regex_date(body_text)
    if sub:
        d = normalize_date(sub, reference_now)
        if d:
            return d

    m = URL_DATE_PATTERN.search(url)
    if m:
        try:
            y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(y, mo, da).strftime("%Y-%m-%d")
        except Exception:
            pass

    return None


def extract_title_from_html(html):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    for block in _get_jsonld_blocks(soup):
        if isinstance(block, dict) and block.get("headline"):
            return str(block["headline"]).strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return ""


# =============================================================================
# SITEMAP / FEED DISCOVERY
# =============================================================================
def _normalize_url(u, base=None):
    if base:
        u = urljoin(base, u)
    u, _ = urldefrag(u)
    return u.strip()


def _discover_xml_candidates(site_url):
    parsed = urlparse(site_url if "://" in site_url else "https://" + site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    candidates = []

    robots = fetch_many([base + "/robots.txt"])
    robots_body = robots.get(base + "/robots.txt")
    if robots_body:
        for line in robots_body.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(_normalize_url(line.split(":", 1)[1].strip()))

    for path in COMMON_SITEMAP_PATHS + COMMON_FEED_PATHS:
        candidates.append(base + path)

    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return base, ordered


def _looks_like_xml(text):
    if not text:
        return False
    head = text.lstrip()[:200].lower()
    return (head.startswith("<?xml") or "<urlset" in head or
            "<sitemapindex" in head or "<rss" in head or "<feed" in head)


def _parse_sitemap_or_index(xml_text):
    """Returns (kind, data):
    'index'  → list of child sitemap URLs
    'urlset' → [{url, date_raw}, ...]
    'rss'    → [{url, date_raw}, ...]
    'feed'   → [{url, date_raw}, ...]
    None     → not parseable
    """
    try:
        root = etree.fromstring(xml_text.encode("utf-8", errors="ignore"))
    except Exception:
        return None, []

    tag = etree.QName(root).localname.lower()

    if tag == "sitemapindex":
        children = []
        for sm in root.findall("sm:sitemap", SITEMAP_NS) or root.findall("{*}sitemap"):
            loc = sm.find("sm:loc", SITEMAP_NS)
            if loc is None:
                loc = sm.find("{*}loc")
            if loc is not None and loc.text:
                children.append(_normalize_url(loc.text.strip()))
        return "index", children

    if tag == "urlset":
        rows = []
        for url_el in root.findall("sm:url", SITEMAP_NS) or root.findall("{*}url"):
            loc = url_el.find("sm:loc", SITEMAP_NS)
            if loc is None:
                loc = url_el.find("{*}loc")
            if loc is None or not loc.text:
                continue
            lastmod = url_el.find("sm:lastmod", SITEMAP_NS)
            if lastmod is None:
                lastmod = url_el.find("{*}lastmod")
            date_raw = lastmod.text.strip() if lastmod is not None and lastmod.text else None
            rows.append({"url": _normalize_url(loc.text.strip()), "date_raw": date_raw})
        return "urlset", rows

    if tag == "rss":
        rows = []
        for item in root.findall(".//item"):
            link = item.find("link")
            pub = item.find("pubDate")
            if link is None or not (link.text or "").strip():
                continue
            rows.append({
                "url": _normalize_url(link.text.strip()),
                "date_raw": pub.text.strip() if pub is not None and pub.text else None,
            })
        return "rss", rows

    if tag == "feed":  # Atom
        ns_atom = {"a": "http://www.w3.org/2005/Atom"}
        rows = []
        for entry in root.findall("a:entry", ns_atom) or root.findall("{*}entry"):
            link_el = entry.find("a:link", ns_atom)
            if link_el is None:
                link_el = entry.find("{*}link")
            href = link_el.get("href") if link_el is not None else None
            updated = entry.find("a:updated", ns_atom)
            if updated is None:
                updated = entry.find("{*}updated")
            published = entry.find("a:published", ns_atom)
            if published is None:
                published = entry.find("{*}published")
            date_raw = (
                (published.text if published is not None else None) or
                (updated.text if updated is not None else None)
            )
            if href:
                rows.append({"url": _normalize_url(href.strip()), "date_raw": date_raw})
        return "feed", rows

    return None, []


def _resolve_all_leaf_entries(candidate_xml_urls, max_index_depth=2):
    """Fetch XML candidates; follow sitemapindex → child sitemaps recursively.
    Returns (found_any_xml: bool, entries: [{url, date_raw}])."""
    fetched = fetch_many(candidate_xml_urls)

    for cand in candidate_xml_urls:
        body = fetched.get(cand)
        if not _looks_like_xml(body):
            continue

        kind, data = _parse_sitemap_or_index(body)
        if kind is None:
            continue

        if kind in ("urlset", "rss", "feed"):
            return True, data

        if kind == "index":
            leaf_entries = []
            frontier = list(data)
            depth = 0
            while frontier and depth < max_index_depth:
                child_fetched = fetch_many(frontier)
                next_frontier = []
                for child_url in frontier:
                    child_body = child_fetched.get(child_url)
                    if not _looks_like_xml(child_body):
                        continue
                    child_kind, child_data = _parse_sitemap_or_index(child_body)
                    if child_kind in ("urlset", "rss", "feed"):
                        leaf_entries.extend(child_data)
                    elif child_kind == "index":
                        next_frontier.extend(child_data)
                frontier = next_frontier
                depth += 1
            return True, leaf_entries

    return False, []


# =============================================================================
# STRUCTURE DETECTION (Case 1 vs Case 2)
# =============================================================================
def _entries_are_structured(entries):
    """Case 1 if ≥90% of entries already carry a parseable date."""
    if not entries:
        return False
    dated = sum(1 for e in entries if e.get("date_raw") and normalize_date(e["date_raw"]))
    return (dated / len(entries)) >= STRUCTURED_DATE_COVERAGE_THRESHOLD


# =============================================================================
# DATE-RANGE HELPER
# =============================================================================
def _in_date_range(date_str, cutoff_dt, end_dt=None):
    """Return True if date_str (YYYY-MM-DD) is within [cutoff_dt.date, end_dt.date]."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    start = _to_date(cutoff_dt)
    end = _to_date(end_dt) if end_dt else datetime.now().date()
    return start <= d <= end


# =============================================================================
# CASE 1 — Structured sitemap: trust XML dates, no page visits needed
# =============================================================================
def _run_case1(entries, cutoff_dt, end_dt, plan_name, plan_id=None):
    kept = []
    for e in entries:
        d = normalize_date(e.get("date_raw"))
        if _in_date_range(d, cutoff_dt, end_dt):
            kept.append({"url": e["url"], "date": d})

    # De-duplicate
    seen, deduped = set(), []
    for row in kept:
        if row["url"] not in seen:
            seen.add(row["url"])
            deduped.append(row)

    _log(f"Case 1: {len(deduped)} URL(s) in date range out of {len(entries)} sitemap entries.", plan_name, "crawl")
    if plan_id:
        update_crawl_progress(plan_id, f"📥 Downloading {len(deduped)} page(s) in parallel...", 30)

    # Fetch pages only for title and summary — fetch failure never removes a URL
    htmls = fetch_many([r["url"] for r in deduped], plan_name)
    
    if plan_id:
        update_crawl_progress(plan_id, f"✍️ Extracting content & summarizing {len(deduped)} page(s)...", 60)

    records = []
    completed_urls = []
    failed_urls = []
    for index, row in enumerate(deduped):
        url = row["url"]
        if plan_id:
            pct = 60 + int((index / len(deduped)) * 30)
            msg = f"✍️ [{index+1}/{len(deduped)}] Summarizing: {url}"
            update_crawl_progress(plan_id, msg, pct, url_index=index, completed_urls=completed_urls, failed_urls=failed_urls)
            _log(msg, plan_name, "ai")
            
        html = htmls.get(url)
        article = extract_article_with_newspaper(url, html) if html else None
        title = article["title"] if article else ""
        text = article["text"] if article else ""
        
        if text:
            summary_str = summarize(text)
            completed_urls.append(url)
        else:
            summary_str = ""
            failed_urls.append(url)
            
        records.append({
            "Published Date": row["date"],
            "Title": title or extract_title_from_html(html) if html else "",
            "URL": url,
            "Summary": summary_str,
            "Content": text
        })
    return records


# =============================================================================
# CASE 2 — Unstructured sitemap: visit every URL, extract date from page
# =============================================================================
def _run_case2(entries, cutoff_dt, end_dt, plan_name, plan_id=None):
    urls = []
    seen = set()
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            urls.append(e["url"])

    _log(f"Case 2: fetching all {len(urls)} sitemap/feed URL(s) to read their published date …", plan_name, "crawl")
    if plan_id:
        update_crawl_progress(plan_id, f"📥 Fetching sitemap pages to read dates ({len(urls)} URLs)...", 20)
        
    htmls = fetch_many(urls, plan_name)

    if plan_id:
        update_crawl_progress(plan_id, f"🔍 Inspecting dates & extracting {len(urls)} page(s)...", 50)

    records = []
    checked = matched = 0
    completed_urls = []
    failed_urls = []
    
    for index, url in enumerate(urls):
        if plan_id:
            pct = 50 + int((index / len(urls)) * 40)
            msg = f"🔍 [{index+1}/{len(urls)}] Inspecting date: {url}"
            update_crawl_progress(plan_id, msg, pct, url_index=index, completed_urls=completed_urls, failed_urls=failed_urls)
            _log(msg, plan_name, "crawl")
            
        html = htmls.get(url)
        if not html:
            failed_urls.append(url)
            continue
        checked += 1
        article = extract_article_with_newspaper(url, html)
        if not article:
            failed_urls.append(url)
            continue
        date = article["date"]
        if _in_date_range(date, cutoff_dt, end_dt):
            matched += 1
            completed_urls.append(url)
            records.append({
                "Published Date": date,
                "Title": article["title"],
                "URL": url,
                "Summary": summarize(article["text"]),
                "Content": article["text"]
            })
        else:
            failed_urls.append(url)

    _log(f"Case 2: {matched} URL(s) matched the date range out of {checked} page(s) fetched.", plan_name, "crawl")
    return records


# =============================================================================
# CASE 3 / BFS DIRECT — BFS crawl, store all pages, extract date from each
# =============================================================================
def _extract_links(html, base_url):
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    domain = urlparse(base_url).netloc
    links = []
    for a in soup.find_all("a", href=True):
        href = _normalize_url(a["href"], base=base_url)
        if not href.startswith("http"):
            continue
        if urlparse(href).netloc != domain:
            continue
        low = href.lower()
        if low.endswith(SKIP_EXTENSIONS):
            continue
        if any(kw in low for kw in SKIP_URL_KEYWORDS):
            continue
        links.append(href)
    return links


def _run_bfs(start_url, cutoff_dt, end_dt, plan_name, plan_id=None, max_depth=2, max_pages=500, is_esg_mode=False):
    """BFS from start_url. Stores ALL fetched HTML, extracts date+title from each page."""
    visited = set()
    frontier = [_normalize_url(start_url)]
    all_pages = {}   # url → html
    depth = 0

    while frontier and depth <= max_depth and len(visited) < max_pages:
        frontier = [u for u in frontier if u not in visited]
        if not frontier:
            break
        remaining = max_pages - len(visited)
        frontier = frontier[:remaining]

        step_msg = f"🌐 BFS depth {depth}: crawling {len(frontier)} page(s) …"
        _log(step_msg, plan_name, "crawl")
        if plan_id:
            pct = 20 + int((depth / (max_depth + 1)) * 40)
            update_crawl_progress(plan_id, step_msg, pct)

        fetched = fetch_many(frontier, plan_name)

        next_frontier = []
        for url in frontier:
            visited.add(url)
            html = fetched.get(url)
            if not html:
                continue
            all_pages[url] = html           # keep HTML for date/title extraction
            if depth < max_depth:
                next_frontier.extend(_extract_links(html, url))

        frontier = list(dict.fromkeys(next_frontier))
        depth += 1

    _log(f"BFS: {len(all_pages)} page(s) fetched across depth 0–{max_depth}.", plan_name, "crawl")
    if plan_id:
        update_crawl_progress(plan_id, f"🔍 Extracted {len(all_pages)} total page(s). Processing dates & summaries...", 60)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    records = []
    completed_urls = []
    failed_urls = []
    all_items = list(all_pages.items())

    def process_single(item_info):
        idx, url, html = item_info
        try:
            article = extract_article_with_newspaper(url, html)
            if not article:
                return idx, url, None, "extract_failed"
            
            title = article.get("title", "")
            text = article.get("text", "")
            date = article.get("date")

            if not _in_date_range(date, cutoff_dt, end_dt):
                return idx, url, None, "date_out_of_range"

            if is_esg_mode:
                if not text:
                    return idx, url, None, "empty_text"
                # Check ESG relevance (this might call LLM, but GIL is released or we run in thread)
                if not is_environment_related(title, text):
                    return idx, url, None, "not_esg_related"
                summary_str = generate_summary_llm(title, text)
            else:
                summary_str = summarize(text)

            record = {
                "Published Date": date,
                "Title": title,
                "URL": url,
                "Summary": summary_str,
                "Content": text
            }
            return idx, url, record, "success"
        except Exception as e:
            return idx, url, None, f"error: {str(e)}"

    completed_count = 0
    with ThreadPoolExecutor(max_workers=16) as executor:
        # submit tasks
        futures = {
            executor.submit(process_single, (i, url, html)): (i, url)
            for i, (url, html) in enumerate(all_items)
        }
        
        for future in as_completed(futures):
            i, url = futures[future]
            try:
                idx, url, record, status = future.result()
            except Exception as e:
                record = None
                status = f"future_error: {str(e)}"
                
            completed_count += 1
            
            # Progress update and logging
            if plan_id:
                pct = 60 + int((completed_count / len(all_items)) * 30)
                msg = f"✍️ [{completed_count}/{len(all_items)}] Processing article: {url}"
                update_crawl_progress(plan_id, msg, pct, url_index=completed_count, completed_urls=completed_urls, failed_urls=failed_urls)
                _log(msg, plan_name, "ai")
                
            if status == "success" and record:
                completed_urls.append(url)
                records.append(record)
            else:
                failed_urls.append(url)

    _log(f"BFS: {len(records)} page(s) matched the date range.", plan_name, "crawl")
    return records


# =============================================================================
# SAVE RECORDS TO DB
# =============================================================================
def _save_records(records, plan, plan_name, plan_id, prompt_enabled, prompt, threshold, end_dt):
    """Apply relevance filter then persist articles to the database."""
    # Relevance filtering (only when prompt is enabled)
    if prompt and prompt_enabled and records:
        filtered = []
        for r in records:
            title = r.get("Title", "")
            content = r.get("Content", "")
            score = calculate_relevance_score(prompt, title, content)
            r["relevance_score"] = score
            r["relevance_reason"] = f"Relevance match: {score:.1f}%"
            
            # Check for direct keyword/substring match as fallback
            text_lower = f"{title}\n{content}".lower()
            prompt_words = [w.strip() for w in re.split(r'[\s,;|]+', prompt.lower()) if len(w.strip()) > 2]
            direct_match = False
            if prompt_words:
                for w in prompt_words:
                    if w in text_lower:
                        direct_match = True
                        break
            else:
                direct_match = True
                
            if score > 0 or direct_match:
                filtered.append(r)
        _log(
            f"Relevance filter: {len(records)} → {len(filtered)} article(s) matching prompt criteria.",
            plan_name, "ai"
        )
        records = filtered

    # Custom end-date filter
    if end_dt is not None:
        before = len(records)
        records = [
            r for r in records
            if not r.get("Published Date") or
            datetime.strptime(r["Published Date"], "%Y-%m-%d") <= end_dt
        ]
        if before != len(records):
            _log(f"Custom end-date filter: {before} → {len(records)} article(s).", plan_name, "crawl")

    saved = 0
    saved_articles = []
    for r in records:
        url = r.get("URL", "")
        if not url or is_seen_url(plan_id, url):
            continue
        import uuid
        article = {
            "id": f"art_{uuid.uuid4().hex[:12]}",
            "plan_id": plan_id,
            "title": r.get("Title", ""),
            "url": url,
            "publishedDate": r.get("Published Date", ""),
            "summary": r.get("Summary", ""),
            "content": "",
            "source": "",
            "canonicalUrl": url,
            "metaDescription": "",
            "createdAt": datetime.utcnow().isoformat() + "Z",
            "metadata": {
                "relevance_score": r.get("relevance_score", 100),
                "relevance_reason": r.get("relevance_reason", "Matched date range"),
            }
        }
        save_article(article)
        add_seen_url(plan_id, url)
        saved += 1
        saved_articles.append(article)

    return saved_articles


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def run_crawl_backend(plan_id: str):
    """Called by the scheduler and /api/plans/{id}/run endpoint."""
    with _running_lock:
        if plan_id in _running_plans:
            log.info("Plan %s already running — skipping.", plan_id)
            return
        _running_plans.add(plan_id)

    try:
        plan = get_plan(plan_id)
        if not plan:
            log.error("Plan %s not found.", plan_id)
            return

        plan_name = plan.get("name", plan_id)
        urls = plan.get("urls", [])
        if not urls:
            _log("No URLs configured for this plan.", plan_name, "warn")
            return

        # ── Read URL config ────────────────────────────────────────────────
        first = urls[0]
        if isinstance(first, str):
            site_url = first
            crawl_method = "default"
            bfs_depth = 2
        else:
            site_url = first.get("url", "")
            crawl_method = first.get("crawlMethod", "default")
            bfs_depth = int(first.get("depth") or 2)

        if not site_url:
            _log("First URL is empty.", plan_name, "warn")
            return

        cutoff_dt = _date_cutoff_for_plan(plan)
        end_dt = _custom_end_for_plan(plan)
        now = datetime.now()

        prompt_enabled = plan.get("promptEnabled", True)
        prompt = plan.get("prompt", "") if prompt_enabled else ""
        threshold = float(plan.get("relevanceThreshold", 70))

        update_crawl_progress(plan_id, "🚀 Initializing crawler...", 5)
        _log(
            f"Crawl started | Method: {'BFS Direct (depth ' + str(bfs_depth) + ')' if crawl_method == 'bfs' else 'Default (auto-chain)'} | URL: {site_url}",
            plan_name, "crawl"
        )

        # ── BFS DIRECT mode ────────────────────────────────────────────────
        if crawl_method == "bfs":
            _log(f"Running BFS Direct (ESG Agent Mode) at depth {bfs_depth} …", plan_name, "crawl")
            update_crawl_progress(plan_id, f"🌐 Running BFS Direct (depth {bfs_depth})...", 10)
            records = _run_bfs(site_url, cutoff_dt, end_dt, plan_name, plan_id, max_depth=bfs_depth, is_esg_mode=True)

            if not records:
                _log("BFS Direct: no pages matched the date range & ESG criteria. Try increasing depth or switching to Default.", plan_name, "warn")
                update_crawl_progress(plan_id, "⚠️ BFS Direct complete: no matching pages found.", 100, is_active=False)
                return

        # ── DEFAULT mode: auto-chain Sitemap → RSS fallback → BFS fallback ─
        else:
            _log("Discovering XML sitemap/feed …", plan_name, "crawl")
            update_crawl_progress(plan_id, "🔍 Discovering XML sitemap/feed...", 10)
            base_url, xml_candidates = _discover_xml_candidates(site_url)
            found_xml, entries = _resolve_all_leaf_entries(xml_candidates)

            if found_xml and entries:
                if _entries_are_structured(entries):
                    _log(
                        f"Case 1 (structured sitemap): {len(entries)} entries with reliable dates. Trusting XML directly.",
                        plan_name, "crawl"
                    )
                    update_crawl_progress(plan_id, f"📄 Structured sitemap: {len(entries)} entries found. Trusting XML dates.", 15)
                    records = _run_case1(entries, cutoff_dt, end_dt, plan_name, plan_id)
                else:
                    _log(
                        f"Case 2 (unstructured sitemap): {len(entries)} entries, dates missing in XML. Visiting every page.",
                        plan_name, "crawl"
                    )
                    update_crawl_progress(plan_id, f"📄 Unstructured sitemap: {len(entries)} entries. Inspecting pages for dates...", 15)
                    records = _run_case2(entries, cutoff_dt, end_dt, plan_name, plan_id)
            else:
                _log(
                    f"Case 3 (no sitemap/feed found for {base_url}). Falling back to BFS crawl depth 2.",
                    plan_name, "crawl"
                )
                update_crawl_progress(plan_id, "🌐 No sitemap found. Falling back to BFS crawl (Depth 2)...", 15)
                records = _run_bfs(site_url, cutoff_dt, end_dt, plan_name, plan_id, max_depth=2, is_esg_mode=False)

        _log(f"Crawl complete. Saving {len(records)} article(s) to database …", plan_name, "crawl")
        update_crawl_progress(plan_id, f"💾 Filtering & saving {len(records)} article(s) to database...", 90)
        newly_saved = _save_records(records, plan, plan_name, plan_id, prompt_enabled, prompt, threshold, end_dt)
        saved = len(newly_saved)
        _log(f"✅ Done. {saved} new article(s) saved.", plan_name, "crawl")
        update_crawl_progress(plan_id, f"✅ Complete! Saved {saved} new article(s).", 100, is_active=False)

        # ── Auto-mail: "immediate" mode sends right after this crawl ────────
        # (mirrors the old browser-driven behaviour, now running fully server-side)
        if newly_saved and plan.get("autoMail"):
            send_mode = plan.get("sendMode") or "immediate"
            if send_mode == "immediate":
                try:
                    from backend.email_service import send_digest_for_plan
                    active_groups = [g for g in (plan.get("recipientGroups") or []) if g.get("active")]
                    recipients = sorted({e for g in active_groups for e in (g.get("emails") or [])})
                    if recipients:
                        _log(f"📧 Auto-mail (immediate): sending {len(newly_saved)} article(s) to {len(recipients)} recipient(s)…", plan_name, "email")
                        result = send_digest_for_plan(plan, newly_saved)
                        _log(f"✅ Auto-mail complete: {result['sent']} sent, {result['failed']} failed.", plan_name, "email")
                    else:
                        _log("⚠️ Auto-mail ON but no active recipients — add emails in the Email tab.", plan_name, "warn")
                except Exception as e:
                    log.exception("Auto-mail (immediate) failed for plan %s: %s", plan_id, e)
                    _log(f"❌ Auto-mail failed: {e}", plan_name, "error")

    except Exception as e:
        log.exception("Crawl error for plan %s: %s", plan_id, e)
        _log(f"Crawl error: {e}", plan_name, "error")
        update_crawl_progress(plan_id, f"❌ Crawl failed: {str(e)}", 100, is_active=False)
    finally:
        with _running_lock:
            _running_plans.discard(plan_id)


# =============================================================================
# COMPATIBILITY HELPERS (imported by main.py)
# =============================================================================
def fetch_url_html(url: str):
    """Fetch a single URL and return (html_text, headers_dict).
    Returns (None, {}) on failure."""
    try:
        results = fetch_many([url])
        html = results.get(url)
        return (html, {}) if html else (None, {})
    except Exception:
        return None, {}


def is_allowed_by_robots(url: str) -> bool:
    """Check robots.txt for the given URL.
    Returns True if crawling is allowed (or robots.txt not found)."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        results = fetch_many([robots_url])
        body = results.get(robots_url)
        if not body:
            return True
        # Very simple check: look for Disallow rules for our bot or *
        path = parsed.path or "/"
        for line in body.splitlines():
            line = line.strip()
            if line.lower().startswith("disallow:"):
                disallowed = line.split(":", 1)[1].strip()
                if disallowed and path.startswith(disallowed):
                    return False
        return True
    except Exception:
        return True
