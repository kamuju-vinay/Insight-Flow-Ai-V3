# =============================================================================
# config.py - All tunables for the Hybrid Article Crawler
# Ported from notebook Cell 2 + Cell 2b
# =============================================================================
import re
from datetime import timedelta

# HTTP / ASYNC SETTINGS
USER_AGENT = (
    "Mozilla/5.0 (compatible; ArticleDiscoveryEngine/1.0; "
    "+https://example.com/bot-info)"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT_SECONDS = 10
MAX_CONCURRENT_REQUESTS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5
MAX_PAGES_TO_INSPECT = 400

# SITEMAP DISCOVERY
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",
    "/sitemapindex.xml",
    "/news-sitemap.xml",
    "/sitemap1.xml",
]

# URL FILTERING
NON_PAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".mp3", ".mp4", ".mov", ".avi",
    ".xml", ".json", ".rss", ".atom", ".txt",
)

# ARTICLE CLASSIFICATION THRESHOLDS
MIN_WORD_COUNT_FOR_ARTICLE = 120
MIN_PARAGRAPHS_FOR_ARTICLE = 2
ARTICLE_SIGNAL_TOTAL = 11
ARTICLE_SIGNAL_MAJORITY = 5
STRONG_SIGNAL_MIN_WORDS = 60
MIN_SLUG_WORDS_FOR_ARTICLE = 3

# SUMMARY SETTINGS
SUMMARY_MAX_WORDS = 250
SUMMARY_MIN_SENTENCES = 3
SUMMARY_MAX_SENTENCES = 6

# DEDUPLICATION
TITLE_SIMILARITY_THRESHOLD = 0.90
SIMHASH_HAMMING_THRESHOLD = 3

# DATE PATTERNS (Cell 2b)
DATE_REGEX_PATTERNS = [
    re.compile(r"\b\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}\b"),
    re.compile(r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"),
    re.compile(r"\b\d{1,2}[\-\s][A-Za-z]{3,9}[\-\s]\d{4}\b"),
    re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\b"),
    re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:IST|UTC|GMT|EST|PST)?\b"),
]

DATE_LABEL_PHRASES = (
    "published", "published on", "publish date", "published date",
    "publication date", "date published", "posted", "posted on",
    "release date", "released", "created", "created on",
    "first published", "originally published",
    "updated", "updated:", "last updated", "modified", "modified on",
    "revised", "edited", "updated at",
)

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

DATE_META_PROPERTY_NAMES = [
    "article:published_time", "article:modified_time", "og:updated_time",
]
DATE_META_NAME_NAMES = [
    "publish-date", "publish_date", "publishdate", "pubdate", "date",
    "dc.date", "dc.date.created", "dc.date.modified", "dc.Date",
    "sailthru.date", "parsely-pub-date", "datePublished", "dateModified",
]
JSONLD_DATE_KEYS = ["datePublished", "dateCreated", "uploadDate", "dateModified"]

# RSS / Atom feed discovery
FEED_LINK_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
COMMON_FEED_PATHS = [
    "/feed/", "/feed", "/rss/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/index.xml", "/feeds/posts/default", "/comments/feed/",
    "/blog/feed/", "/blog/rss/", "/news/feed/", "/?feed=rss2",
]
RSS_NS_ATOM = {"atom": "http://www.w3.org/2005/Atom"}

# BFS crawler
TRACKING_PARAMS = {
    "utm_source", "utm_campaign", "utm_medium", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "igshid",
}

ALLOW_FEED_ONLY_FALLBACK = False