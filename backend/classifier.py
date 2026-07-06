# =============================================================================
# classifier.py - Article Classification (11 content signals)
# Ported from notebook Cell 6
# =============================================================================
import re
import urllib.parse as urlparse
import logging

from backend.config import (
    MIN_WORD_COUNT_FOR_ARTICLE, MIN_PARAGRAPHS_FOR_ARTICLE,
    ARTICLE_SIGNAL_MAJORITY, STRONG_SIGNAL_MIN_WORDS,
    MIN_SLUG_WORDS_FOR_ARTICLE,
)
from backend.date_extractor import get_jsonld_blocks

log = logging.getLogger(__name__)


def jsonld_type_matches(block, *type_names):
    t = block.get("@type")
    if t is None:
        return False
    types = t if isinstance(t, list) else [t]
    types_lower = [str(x).lower() for x in types]
    return any(name.lower() in types_lower for name in type_names)


def _readability_score(word_count, sentence_count):
    """Genuine articles: ~10-35 words/sentence. Nav/boilerplate: very short or giant blobs."""
    if sentence_count == 0:
        return 0.0
    avg = word_count / sentence_count
    return 1.0 if 8 <= avg <= 40 else 0.0


def _url_structure_looks_like_article(url):
    """URL Structure signal. Does NOT require minimum path depth.
    Many real sites use flat article URLs (domain.com/slug/) without a /blog/ prefix."""
    path = urlparse.urlsplit(url).path.strip("/")
    if not path:
        return False  # homepage

    if re.search(r"/(19|20)\d{2}[/-](0?[1-9]|1[0-2])[/-]", url):
        return True

    segments = [p for p in path.split("/") if p]
    slug = segments[-1] if segments else ""
    slug_words = [w for w in re.split(r"[-_]", slug) if w]
    return len(slug_words) >= MIN_SLUG_WORDS_FOR_ARTICLE


def _heading_structure_looks_like_article(soup):
    """Genuine articles: exactly one <h1> + at least one <h2>/<h3>."""
    h1_count = len(soup.find_all("h1"))
    subheading_count = len(soup.find_all(["h2", "h3"]))
    return h1_count == 1 and subheading_count >= 1


def _open_graph_looks_like_article(soup):
    """Open Graph signal: og:type == 'article', or populated og:title + og:description."""
    og_type = soup.find("meta", attrs={"property": "og:type"})
    if og_type and og_type.get("content", "").strip().lower() == "article":
        return True
    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    return bool(
        (og_title and og_title.get("content", "").strip())
        and (og_desc and og_desc.get("content", "").strip())
    )


def _date_metadata_present(soup, jsonld_blocks):
    """Publication Date signal. Accepts both published_time and modified_time."""
    if soup.find("meta", attrs={"property": "article:published_time"}) is not None:
        return True
    if soup.find("meta", attrs={"property": "article:modified_time"}) is not None:
        return True
    if any(
        isinstance(b, dict) and (b.get("datePublished") or b.get("dateModified"))
        for b in jsonld_blocks
    ):
        return True
    if soup.find("meta", attrs={"name": "datePublished"}) is not None:
        return True
    return False


def score_article_signals(soup, url):
    """Score a page against 11 independent article signals. Returns (signal_count, details)."""
    jsonld_blocks = get_jsonld_blocks(soup)
    signals = 0
    details = {}

    has_jsonld_article = any(
        jsonld_type_matches(b, "Article") for b in jsonld_blocks if isinstance(b, dict)
    )
    has_jsonld_news = any(
        jsonld_type_matches(b, "NewsArticle", "BlogPosting", "ReportageNewsArticle", "Report")
        for b in jsonld_blocks if isinstance(b, dict)
    )
    signals += int(has_jsonld_article); details["jsonld_article"] = has_jsonld_article
    signals += int(has_jsonld_news);    details["jsonld_news"] = has_jsonld_news

    has_article_tag = soup.find("article") is not None
    signals += int(has_article_tag); details["article_tag"] = has_article_tag

    has_og = _open_graph_looks_like_article(soup)
    signals += int(has_og); details["open_graph"] = has_og

    has_date_metadata = _date_metadata_present(soup, jsonld_blocks)
    signals += int(has_date_metadata); details["date_metadata"] = has_date_metadata

    has_time_tag = soup.find("time", attrs={"datetime": True}) is not None
    signals += int(has_time_tag); details["time_tag"] = has_time_tag

    has_heading_structure = _heading_structure_looks_like_article(soup)
    signals += int(has_heading_structure); details["heading_structure"] = has_heading_structure

    has_url_structure = _url_structure_looks_like_article(url)
    signals += int(has_url_structure); details["url_structure"] = has_url_structure

    title_tag = soup.find("title")
    has_title = title_tag is not None and title_tag.get_text(strip=True) != ""
    signals += int(has_title); details["title_tag"] = has_title

    paragraphs = soup.find_all("p")
    body_text = " ".join(p.get_text(" ", strip=True) for p in paragraphs)
    word_count = len(body_text.split())
    sentence_count = max(1, len(re.split(r"[.!?]+", body_text)))
    density_ok = (
        word_count >= MIN_WORD_COUNT_FOR_ARTICLE
        and len(paragraphs) >= MIN_PARAGRAPHS_FOR_ARTICLE
        and _readability_score(word_count, sentence_count) == 1.0
    )
    signals += int(density_ok); details["content_density"] = density_ok
    details["word_count"] = word_count
    details["paragraph_count"] = len(paragraphs)
    details["jsonld_blocks"] = jsonld_blocks

    return signals, details


def classify_article(html, soup, url):
    """Public entry point. Returns (is_article: bool, details: dict).

    Two decision paths:
    1. STRONG DECLARATION FAST PATH -- JSON-LD Article/NewsArticle or og:type=article,
       plus a real title and STRONG_SIGNAL_MIN_WORDS of body text -> trust it.
    2. MAJORITY VOTE FALLBACK -- ARTICLE_SIGNAL_MAJORITY out of 11 signals required.
    """
    signal_count, details = score_article_signals(soup, url)
    jsonld_blocks = details.pop("jsonld_blocks", [])

    strong_declaration = (
        details["jsonld_article"] or details["jsonld_news"] or details["open_graph"]
    )
    has_enough_text = (
        details["word_count"] >= STRONG_SIGNAL_MIN_WORDS
        and details["paragraph_count"] >= 1
    )

    if strong_declaration and details["title_tag"] and has_enough_text:
        is_article = True
        details["decision_path"] = "strong_declaration"
    else:
        is_article = signal_count >= ARTICLE_SIGNAL_MAJORITY
        details["decision_path"] = "majority_vote"

    details["signal_count"] = signal_count
    return is_article, details


def calculate_relevance_score(prompt: str, title: str, content: str) -> float:
    """Calculate cosine similarity score from 0 to 100 between prompt and article."""
    if not prompt or not (title or content):
        return 100.0  # default to relevant if no prompt is defined
    
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        
        # Combine title and content
        article_text = f"{title}\n\n{content}"
        vectorizer = TfidfVectorizer(stop_words='english')
        tfidf = vectorizer.fit_transform([prompt, article_text])
        cos_sim = float(cosine_similarity(tfidf[0:1], tfidf[1:2])[0][0])
        
        # Scale typical TF-IDF cosine similarities to 0-100 range.
        # A similarity of 0.28+ is a very strong match for short-long text matching.
        # So we can scale it: score = cos_sim * 250 (capped at 100).
        score = min(100.0, cos_sim * 250.0)
        return score
    except Exception as e:
        log.warning("Failed to calculate cosine similarity: %s. Falling back to term overlap.", e)
        # Fallback to term overlap ratio
        text = f"{title} {content}".lower()
        prompt_words = [w for w in re.findall(r'\w+', prompt.lower()) if len(w) > 2]
        if not prompt_words:
            return 100.0
        matches = sum(1 for w in prompt_words if w in text)
        return (matches / len(prompt_words)) * 100.0

