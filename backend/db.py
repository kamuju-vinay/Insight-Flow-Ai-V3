import os
import json
from datetime import datetime

from supabase import create_client, Client

# =============================================================================
# db.py - Supabase (Postgres) backed data layer
#
# Drop-in replacement for the old SQLite version. Every function name,
# parameter list, and return shape (Python dicts with the SAME camelCase
# keys the rest of the app already expects: recipientGroups, articlesCount,
# pubDate, etc.) is kept identical, so main.py / crawler.py / email_service.py
# do not need any changes.
#
# Under the hood, the Postgres columns use snake_case (Postgres best
# practice / avoids case-folding surprises), and this file translates
# between snake_case (DB) <-> camelCase (rest of the app) transparently.
#
# REQUIRED ENV VARS (set these in Railway / your host):
#   SUPABASE_URL          - e.g. https://xxxxxxxx.supabase.co
#   SUPABASE_SERVICE_KEY  - the "service_role" key from
#                            Supabase > Project Settings > API
#                            (server-side only, never expose to the frontend)
#
# REQUIRED ONE-TIME SETUP:
#   Run the SQL in supabase_schema.sql (in the project root) once, in the
#   Supabase SQL Editor, to create the tables. This file does NOT create
#   tables itself (Supabase's REST API doesn't support DDL).
# =============================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL / SUPABASE_SERVICE_KEY environment variables. "
        "Set them in your host's environment (e.g. Railway Variables) before starting the app."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# A value that can never collide with a real primary key, used as the
# "match everything" filter PostgREST needs for bulk deletes.
_NEVER_MATCH = "___never_matches___"


# -----------------------------------------------------------------------
# Field name maps: DB (snake_case) <-> app (camelCase)
# -----------------------------------------------------------------------

PLAN_JSON_FIELDS = {"urls", "recipientGroups", "periods", "triggerTimes", "crawlState", "schedWeekDays"}
PLAN_BOOL_FIELDS = {"continuousRun", "searchBodyKeywords", "enableAIKeywords", "promptEnabled", "autoMail"}

PLAN_DB_TO_PY = {
    "id": "id", "name": "name", "icon": "icon", "bg": "bg", "status": "status", "stage": "stage",
    "urls": "urls", "recipient_groups": "recipientGroups", "periods": "periods",
    "trigger_times": "triggerTimes", "prompt": "prompt", "keywords": "keywords",
    "articles_count": "articlesCount", "emails_count": "emailsCount", "last_run": "lastRun",
    "created_at": "createdAt", "continuous_run": "continuousRun",
    "relevance_threshold": "relevanceThreshold", "search_body_keywords": "searchBodyKeywords",
    "enable_ai_keywords": "enableAIKeywords", "crawl_state": "crawlState",
    "fetch_period": "fetchPeriod", "fetch_period_days": "fetchPeriodDays",
    "prompt_enabled": "promptEnabled", "sched_freq": "schedFreq", "sched_time": "schedTime",
    "sched_week_days": "schedWeekDays", "sched_month_day": "schedMonthDay",
    "interval_minutes": "intervalMinutes", "sched_custom_unit": "schedCustomUnit",
    "sched_tz": "schedTz", "auto_mail": "autoMail", "send_mode": "sendMode", "send_time": "sendTime",
}
PLAN_PY_TO_DB = {v: k for k, v in PLAN_DB_TO_PY.items()}

ARTICLE_JSON_FIELDS = {"tags", "images", "videos", "attachments", "keywords", "metadata"}

ARTICLE_DB_TO_PY = {
    "id": "id", "plan_id": "plan_id", "url": "url", "title": "title", "subtitle": "subtitle",
    "pub_date": "pubDate", "modified_date": "modifiedDate", "author": "author",
    "category": "category", "tags": "tags", "summary": "summary", "content": "content",
    "images": "images", "videos": "videos", "attachments": "attachments", "language": "language",
    "keywords": "keywords", "canonical_url": "canonicalUrl", "meta_description": "metaDescription",
    "metadata": "metadata", "created_at": "createdAt",
}
ARTICLE_PY_TO_DB = {v: k for k, v in ARTICLE_DB_TO_PY.items()}


def _plan_row_to_py(row):
    plan = {}
    for db_key, py_key in PLAN_DB_TO_PY.items():
        plan[py_key] = row.get(db_key)
    for f in PLAN_JSON_FIELDS:
        if plan.get(f) is None:
            plan[f] = {} if f in ("triggerTimes", "crawlState") else []
    for f in PLAN_BOOL_FIELDS:
        plan[f] = bool(plan.get(f))
    if row.get("prompt_enabled") is None:
        plan["promptEnabled"] = True
    return plan


def _plan_py_to_row(plan):
    row = {}
    for py_key, db_key in PLAN_PY_TO_DB.items():
        if py_key in plan:
            row[db_key] = plan.get(py_key)
    # Defaults / coercions matching old sqlite behavior
    row["urls"] = plan.get("urls", [])
    row["recipient_groups"] = plan.get("recipientGroups", [])
    row["periods"] = plan.get("periods", [])
    row["trigger_times"] = plan.get("triggerTimes", {})
    row["crawl_state"] = plan.get("crawlState", {})
    row["sched_week_days"] = plan.get("schedWeekDays", [])
    row["articles_count"] = plan.get("articlesCount", 0)
    row["emails_count"] = plan.get("emailsCount", 0)
    row["stage"] = plan.get("stage", "idle")
    row["continuous_run"] = bool(plan.get("continuousRun"))
    row["relevance_threshold"] = plan.get("relevanceThreshold", 70)
    row["search_body_keywords"] = bool(plan.get("searchBodyKeywords"))
    row["enable_ai_keywords"] = bool(plan.get("enableAIKeywords", True))
    row["fetch_period"] = plan.get("fetchPeriod", "week")
    row["fetch_period_days"] = plan.get("fetchPeriodDays", 7)
    row["prompt_enabled"] = bool(plan.get("promptEnabled", True))
    row["auto_mail"] = bool(plan.get("autoMail"))
    return row


def _article_row_to_py(row):
    art = {}
    for db_key, py_key in ARTICLE_DB_TO_PY.items():
        art[py_key] = row.get(db_key)
    art["plan_id"] = row.get("plan_id")
    art["planId"] = row.get("plan_id")
    for f in ARTICLE_JSON_FIELDS:
        if art.get(f) is None:
            art[f] = {} if f == "metadata" else []
    meta = art.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    art["metadata"] = meta
    if isinstance(meta, dict):
        for k, v in meta.items():
            art.setdefault(k, v)
    art["publish_date"] = art.get("pubDate", "")
    return art


def _article_py_to_row(art):
    row = {}
    for py_key, db_key in ARTICLE_PY_TO_DB.items():
        row[db_key] = art.get(py_key)
    row["plan_id"] = art.get("planId") or art.get("plan_id")
    row["tags"] = art.get("tags", [])
    row["images"] = art.get("images", [])
    row["videos"] = art.get("videos", [])
    row["attachments"] = art.get("attachments", [])
    row["keywords"] = art.get("keywords", [])
    row["metadata"] = art.get("metadata", {})
    return row


# -----------------------------------------------------------------------
# init
# -----------------------------------------------------------------------

def init_db():
    """
    Tables live in Supabase/Postgres and are created once via
    supabase_schema.sql in the Supabase SQL Editor (PostgREST has no DDL
    endpoint). This just verifies connectivity on startup.
    """
    try:
        supabase.table("settings").select("key").limit(1).execute()
        print("✅ Connected to Supabase.")
    except Exception as e:
        print(f"❌ Could not reach Supabase tables. Did you run supabase_schema.sql? Error: {e}")
        raise


# -----------------------------------------------------------------------
# settings
# -----------------------------------------------------------------------

def get_settings():
    res = supabase.table("settings").select("key, value").execute()
    rows = res.data or []
    settings = {}
    for r in rows:
        val = r["value"]
        try:
            settings[r["key"]] = json.loads(val)
        except Exception:
            if isinstance(val, str) and val.lower() == "true":
                settings[r["key"]] = True
            elif isinstance(val, str) and val.lower() == "false":
                settings[r["key"]] = False
            else:
                try:
                    settings[r["key"]] = int(val)
                except (ValueError, TypeError):
                    settings[r["key"]] = val
    return settings


def save_settings(cfg):
    rows = []
    for k, v in cfg.items():
        if isinstance(v, (dict, list)):
            val_str = json.dumps(v)
        elif isinstance(v, bool):
            val_str = "true" if v else "false"
        else:
            val_str = str(v)
        rows.append({"key": k, "value": val_str})
    if rows:
        supabase.table("settings").upsert(rows, on_conflict="key").execute()


# -----------------------------------------------------------------------
# plans
# -----------------------------------------------------------------------

def get_plans():
    res = supabase.table("plans").select("*").execute()
    return [_plan_row_to_py(r) for r in (res.data or [])]


def get_plan(plan_id):
    res = supabase.table("plans").select("*").eq("id", plan_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        return None
    return _plan_row_to_py(rows[0])


def save_plan(plan):
    row = _plan_py_to_row(plan)
    supabase.table("plans").upsert(row, on_conflict="id").execute()


def delete_plan(plan_id):
    supabase.table("plans").delete().eq("id", plan_id).execute()
    supabase.table("articles").delete().eq("plan_id", plan_id).execute()
    supabase.table("seen_urls").delete().eq("plan_id", plan_id).execute()


# -----------------------------------------------------------------------
# articles
# -----------------------------------------------------------------------

def get_articles(plan_id=None):
    q = supabase.table("articles").select("*")
    if plan_id:
        q = q.eq("plan_id", plan_id)
    res = q.order("pub_date", desc=True).execute()
    return [_article_row_to_py(r) for r in (res.data or [])]


def save_article(art):
    plan_id = art.get("planId") or art.get("plan_id")
    row = _article_py_to_row(art)
    supabase.table("articles").upsert(row, on_conflict="id").execute()

    count_res = (
        supabase.table("articles")
        .select("id", count="exact")
        .eq("plan_id", plan_id)
        .execute()
    )
    count = count_res.count or 0
    supabase.table("plans").update({"articles_count": count}).eq("id", plan_id).execute()


def delete_articles(plan_id=None, article_id=None):
    if plan_id:
        supabase.table("articles").delete().eq("plan_id", plan_id).execute()
        supabase.table("seen_urls").delete().eq("plan_id", plan_id).execute()
        supabase.table("plans").update({"articles_count": 0}).eq("id", plan_id).execute()
    elif article_id:
        res = supabase.table("articles").select("plan_id, url").eq("id", article_id).limit(1).execute()
        rows = res.data or []
        if rows:
            p_id = rows[0]["plan_id"]
            url = rows[0]["url"]
            supabase.table("articles").delete().eq("id", article_id).execute()
            supabase.table("seen_urls").delete().eq("plan_id", p_id).eq("url", url).execute()
            count_res = (
                supabase.table("articles")
                .select("id", count="exact")
                .eq("plan_id", p_id)
                .execute()
            )
            supabase.table("plans").update({"articles_count": count_res.count or 0}).eq("id", p_id).execute()
    else:
        supabase.table("articles").delete().neq("id", _NEVER_MATCH).execute()
        supabase.table("seen_urls").delete().neq("plan_id", _NEVER_MATCH).execute()
        supabase.table("plans").update({"articles_count": 0}).neq("id", _NEVER_MATCH).execute()


# -----------------------------------------------------------------------
# email log
# -----------------------------------------------------------------------

def get_email_logs():
    res = supabase.table("email_log").select("*").order("ts", desc=True).execute()
    logs = []
    for r in (res.data or []):
        el = dict(r)
        el["planId"] = el.get("plan_id")
        el["planName"] = el.get("plan_name")
        el["articlesCount"] = el.get("articles_count")
        el["messageId"] = el.get("message_id")
        logs.append(el)
    return logs


def save_email_log(el):
    row = {
        "id": el.get("id"),
        "plan_id": el.get("planId") or el.get("plan_id"),
        "plan_name": el.get("planName") or el.get("plan_name"),
        "ts": el.get("ts"),
        "recipient": el.get("recipient"),
        "subject": el.get("subject"),
        "articles_count": el.get("articlesCount") or el.get("articles_count", 0),
        "status": el.get("status"),
        "error": el.get("error"),
        "message_id": el.get("messageId") or el.get("message_id"),
    }
    supabase.table("email_log").insert(row).execute()


# -----------------------------------------------------------------------
# activity log
# -----------------------------------------------------------------------

def get_logs():
    res = supabase.table("activity_log").select("*").order("ts", desc=True).limit(1000).execute()
    out = []
    for r in (res.data or []):
        d = dict(r)
        d["planName"] = d.get("plan_name")
        d["plan"] = d.get("plan_name", "")
        out.append(d)
    return out


def save_log(event, plan_name="", log_type="info"):
    log_id = f"log_{int(datetime.utcnow().timestamp() * 1000)}"
    row = {
        "id": log_id,
        "ts": datetime.utcnow().isoformat() + "Z",
        "event": event,
        "plan_name": plan_name,
        "type": log_type,
    }
    supabase.table("activity_log").insert(row).execute()


def clear_activity_logs():
    supabase.table("activity_log").delete().neq("id", _NEVER_MATCH).execute()


# -----------------------------------------------------------------------
# bulk clear
# -----------------------------------------------------------------------

def clear_all_data():
    supabase.table("plans").delete().neq("id", _NEVER_MATCH).execute()
    supabase.table("articles").delete().neq("id", _NEVER_MATCH).execute()
    supabase.table("email_log").delete().neq("id", _NEVER_MATCH).execute()
    supabase.table("activity_log").delete().neq("id", _NEVER_MATCH).execute()
    supabase.table("seen_urls").delete().neq("plan_id", _NEVER_MATCH).execute()


# -----------------------------------------------------------------------
# seen urls (dedupe / incremental crawl)
# -----------------------------------------------------------------------

def is_seen_url(plan_id, url):
    res = (
        supabase.table("seen_urls")
        .select("plan_id")
        .eq("plan_id", plan_id)
        .eq("url", url)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def add_seen_url(plan_id, url):
    row = {
        "plan_id": plan_id,
        "url": url,
        "crawled_at": datetime.utcnow().isoformat() + "Z",
    }
    supabase.table("seen_urls").upsert(row, on_conflict="plan_id,url").execute()
