import os
import sqlite3
import json
from datetime import datetime

DB_FILE = os.path.join(os.path.dirname(__file__), "..", "crawler.db")
LEGACY_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "db.json")

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Settings table (key-value)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # Plans table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        name TEXT,
        icon TEXT,
        bg TEXT,
        status TEXT,
        stage TEXT,
        urls TEXT,
        recipientGroups TEXT,
        periods TEXT,
        triggerTimes TEXT,
        prompt TEXT,
        keywords TEXT,
        articlesCount INTEGER DEFAULT 0,
        emailsCount INTEGER DEFAULT 0,
        lastRun TEXT,
        createdAt TEXT,
        continuousRun INTEGER DEFAULT 0,
        relevanceThreshold INTEGER DEFAULT 70,
        searchBodyKeywords INTEGER DEFAULT 0,
        enableAIKeywords INTEGER DEFAULT 1,
        crawlState TEXT,
        fetchPeriod TEXT DEFAULT 'week',
        fetchPeriodDays INTEGER DEFAULT 7,
        promptEnabled INTEGER DEFAULT 1
    )
    """)

    # Safe migration for DBs created before promptEnabled or schedule columns existed.
    for col, col_type in [
        ("promptEnabled", "INTEGER DEFAULT 1"),
        ("schedFreq", "TEXT"),
        ("schedTime", "TEXT"),
        ("schedWeekDays", "TEXT"),
        ("schedMonthDay", "INTEGER"),
        ("intervalMinutes", "INTEGER"),
        ("schedCustomUnit", "TEXT"),
        ("schedTz", "TEXT"),
        ("autoMail", "INTEGER DEFAULT 0"),
        ("sendMode", "TEXT"),
        ("sendTime", "TEXT")
    ]:
        try:
            cursor.execute(f"ALTER TABLE plans ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Articles table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id TEXT PRIMARY KEY,
        plan_id TEXT,
        url TEXT,
        title TEXT,
        subtitle TEXT,
        pubDate TEXT,
        modifiedDate TEXT,
        author TEXT,
        category TEXT,
        tags TEXT,
        summary TEXT,
        content TEXT,
        images TEXT,
        videos TEXT,
        attachments TEXT,
        language TEXT,
        keywords TEXT,
        canonicalUrl TEXT,
        metaDescription TEXT,
        metadata TEXT,
        createdAt TEXT
    )
    """)
    
    # Email log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_log (
        id TEXT PRIMARY KEY,
        plan_id TEXT,
        plan_name TEXT,
        ts TEXT,
        recipient TEXT,
        subject TEXT,
        articles_count INTEGER,
        status TEXT,
        error TEXT
    )
    """)
    
    # Activity log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_log (
        id TEXT PRIMARY KEY,
        ts TEXT,
        event TEXT,
        planName TEXT,
        type TEXT
    )
    """)
    
    # Seen URLs table for deduplication / incremental crawl
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS seen_urls (
        plan_id TEXT,
        url TEXT,
        crawled_at TEXT,
        PRIMARY KEY (plan_id, url)
    )
    """)
    
    # Upgrade default concurrent workers from 4 to 20 to speed up crawling
    cursor.execute("UPDATE settings SET value = '20' WHERE key = 'concurrent_workers' AND value = '4'")
    
    conn.commit()
    
    # Check if we should migrate legacy db.json
    cursor.execute("SELECT COUNT(*) as cnt FROM plans")
    has_plans = cursor.fetchone()["cnt"] > 0
    
    if not has_plans and os.path.exists(LEGACY_DB_FILE):
        migrate_legacy_data(conn)
        
    conn.close()

def migrate_legacy_data(conn):
    print("🧹 Migrating legacy data from db.json...")
    try:
        with open(LEGACY_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        cursor = conn.cursor()
        
        # 1. Migrate settings
        config = data.get("config", {})
        # ensure new crawler fields are defined with defaults
        config.setdefault("respect_robots_txt", "true")
        config.setdefault("concurrent_workers", "20")
        config.setdefault("timeout", "10")
        config.setdefault("retry_count", "3")
        config.setdefault("delay_between_requests", "1")
        config.setdefault("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        config.setdefault("headers", "{}")
        config.setdefault("proxy", "")
        
        for k, v in config.items():
            val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val_str))
            
        # 2. Migrate plans
        plans = data.get("plans", [])
        for p in plans:
            cursor.execute("""
            INSERT OR REPLACE INTO plans (
                id, name, icon, bg, status, stage, urls, recipientGroups, periods, triggerTimes,
                prompt, keywords, articlesCount, emailsCount, lastRun, createdAt, continuousRun,
                relevanceThreshold, searchBodyKeywords, enableAIKeywords, crawlState, fetchPeriod, fetchPeriodDays
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("id"),
                p.get("name"),
                p.get("icon"),
                p.get("bg"),
                p.get("status"),
                p.get("stage", "idle"),
                json.dumps(p.get("urls", [])),
                json.dumps(p.get("recipientGroups", [])),
                json.dumps(p.get("periods", [])),
                json.dumps(p.get("triggerTimes", {})),
                p.get("prompt"),
                p.get("keywords"),
                p.get("articlesCount", 0),
                p.get("emailsCount", 0),
                p.get("lastRun"),
                p.get("createdAt"),
                1 if p.get("continuousRun") else 0,
                p.get("relevanceThreshold", 70),
                1 if p.get("searchBodyKeywords") else 0,
                1 if p.get("enableAIKeywords") else 0,
                json.dumps(p.get("crawlState", {})),
                p.get("fetchPeriod", "week"),
                p.get("fetchPeriodDays", 7)
            ))
            
        # 3. Migrate articles
        articles = data.get("articles", [])
        for a in articles:
            cursor.execute("""
            INSERT OR REPLACE INTO articles (
                id, plan_id, url, title, subtitle, pubDate, modifiedDate, author, category,
                tags, summary, content, images, videos, attachments, language, keywords,
                canonicalUrl, metaDescription, metadata, createdAt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                a.get("id"),
                a.get("plan_id") or a.get("planId"),
                a.get("url"),
                a.get("title"),
                a.get("subtitle"),
                a.get("pubDate"),
                a.get("modifiedDate"),
                a.get("author"),
                a.get("category"),
                json.dumps(a.get("tags", [])),
                a.get("summary"),
                a.get("content"),
                json.dumps(a.get("images", [])),
                json.dumps(a.get("videos", [])),
                json.dumps(a.get("attachments", [])),
                a.get("language"),
                json.dumps(a.get("keywords", [])),
                a.get("canonicalUrl"),
                a.get("metaDescription"),
                json.dumps(a.get("metadata", {})),
                a.get("createdAt")
            ))
            
        # 4. Migrate emailLog
        email_log = data.get("emailLog", [])
        for el in email_log:
            cursor.execute("""
            INSERT OR REPLACE INTO email_log (
                id, plan_id, plan_name, ts, recipient, subject, articles_count, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                el.get("id"),
                el.get("plan_id") or el.get("planId"),
                el.get("plan_name") or el.get("planName"),
                el.get("ts"),
                el.get("recipient"),
                el.get("subject"),
                el.get("articles_count") or el.get("articlesCount", 0),
                el.get("status"),
                el.get("error")
            ))
            
        # 5. Migrate activityLog
        activity_log = data.get("activityLog", [])
        for al in activity_log:
            cursor.execute("""
            INSERT OR REPLACE INTO activity_log (id, ts, event, planName, type)
            VALUES (?, ?, ?, ?, ?)
            """, (
                al.get("id"),
                al.get("ts"),
                al.get("event"),
                al.get("planName"),
                al.get("type")
            ))
            
        conn.commit()
        print("✅ Migration complete!")
    except Exception as e:
        print(f"❌ Error migrating legacy database: {e}")
        conn.rollback()

def get_settings():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    
    settings = {}
    for r in rows:
        val = r["value"]
        # try to parse as JSON
        try:
            settings[r["key"]] = json.loads(val)
        except:
            if val.lower() == "true":
                settings[r["key"]] = True
            elif val.lower() == "false":
                settings[r["key"]] = False
            else:
                try:
                    settings[r["key"]] = int(val)
                except ValueError:
                    settings[r["key"]] = val
    return settings

def save_settings(cfg):
    conn = get_db_connection()
    cursor = conn.cursor()
    for k, v in cfg.items():
        if isinstance(v, (dict, list)):
            val_str = json.dumps(v)
        elif isinstance(v, bool):
            val_str = "true" if v else "false"
        else:
            val_str = str(v)
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val_str))
    conn.commit()
    conn.close()

def get_plans():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plans")
    rows = cursor.fetchall()
    conn.close()
    
    plans = []
    for r in rows:
        plan = dict(r)
        plan["urls"] = json.loads(plan["urls"] or "[]")
        plan["recipientGroups"] = json.loads(plan["recipientGroups"] or "[]")
        plan["periods"] = json.loads(plan["periods"] or "[]")
        plan["triggerTimes"] = json.loads(plan["triggerTimes"] or "{}")
        plan["crawlState"] = json.loads(plan["crawlState"] or "{}")
        plan["schedWeekDays"] = json.loads(plan.get("schedWeekDays") or "[]")
        plan["autoMail"] = bool(plan.get("autoMail", 0))
        plan["continuousRun"] = bool(plan["continuousRun"])
        plan["searchBodyKeywords"] = bool(plan["searchBodyKeywords"])
        plan["enableAIKeywords"] = bool(plan["enableAIKeywords"])
        plan["promptEnabled"] = bool(plan["promptEnabled"]) if plan.get("promptEnabled") is not None else True
        plans.append(plan)
    return plans

def get_plan(plan_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    plan = dict(row)
    plan["urls"] = json.loads(plan["urls"] or "[]")
    plan["recipientGroups"] = json.loads(plan["recipientGroups"] or "[]")
    plan["periods"] = json.loads(plan["periods"] or "[]")
    plan["triggerTimes"] = json.loads(plan["triggerTimes"] or "{}")
    plan["crawlState"] = json.loads(plan["crawlState"] or "{}")
    plan["schedWeekDays"] = json.loads(plan.get("schedWeekDays") or "[]")
    plan["autoMail"] = bool(plan.get("autoMail", 0))
    plan["continuousRun"] = bool(plan["continuousRun"])
    plan["searchBodyKeywords"] = bool(plan["searchBodyKeywords"])
    plan["enableAIKeywords"] = bool(plan["enableAIKeywords"])
    plan["promptEnabled"] = bool(plan["promptEnabled"]) if plan.get("promptEnabled") is not None else True
    return plan

def save_plan(plan):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO plans (
        id, name, icon, bg, status, stage, urls, recipientGroups, periods, triggerTimes,
        prompt, keywords, articlesCount, emailsCount, lastRun, createdAt, continuousRun,
        relevanceThreshold, searchBodyKeywords, enableAIKeywords, crawlState, fetchPeriod, fetchPeriodDays,
        promptEnabled, schedFreq, schedTime, schedWeekDays, schedMonthDay, intervalMinutes, schedCustomUnit,
        schedTz, autoMail, sendMode, sendTime
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        plan.get("id"),
        plan.get("name"),
        plan.get("icon"),
        plan.get("bg"),
        plan.get("status"),
        plan.get("stage", "idle"),
        json.dumps(plan.get("urls", [])),
        json.dumps(plan.get("recipientGroups", [])),
        json.dumps(plan.get("periods", [])),
        json.dumps(plan.get("triggerTimes", {})),
        plan.get("prompt"),
        plan.get("keywords"),
        plan.get("articlesCount", 0),
        plan.get("emailsCount", 0),
        plan.get("lastRun"),
        plan.get("createdAt"),
        1 if plan.get("continuousRun") else 0,
        plan.get("relevanceThreshold", 70),
        1 if plan.get("searchBodyKeywords") else 0,
        1 if plan.get("enableAIKeywords") else 0,
        json.dumps(plan.get("crawlState", {})),
        plan.get("fetchPeriod", "week"),
        plan.get("fetchPeriodDays", 7),
        1 if plan.get("promptEnabled", True) else 0,
        plan.get("schedFreq"),
        plan.get("schedTime"),
        json.dumps(plan.get("schedWeekDays", [])),
        plan.get("schedMonthDay"),
        plan.get("intervalMinutes"),
        plan.get("schedCustomUnit"),
        plan.get("schedTz"),
        1 if plan.get("autoMail") else 0,
        plan.get("sendMode"),
        plan.get("sendTime")
    ))
    conn.commit()
    conn.close()

def delete_plan(plan_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    cursor.execute("DELETE FROM articles WHERE plan_id = ?", (plan_id,))
    cursor.execute("DELETE FROM seen_urls WHERE plan_id = ?", (plan_id,))
    conn.commit()
    conn.close()

def get_articles(plan_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if plan_id:
        cursor.execute("SELECT * FROM articles WHERE plan_id = ? ORDER BY pubDate DESC", (plan_id,))
    else:
        cursor.execute("SELECT * FROM articles ORDER BY pubDate DESC")
    rows = cursor.fetchall()
    conn.close()
    
    articles = []
    for r in rows:
        art = dict(r)
        art["planId"] = art["plan_id"]
        art["tags"] = json.loads(art["tags"] or "[]")
        art["images"] = json.loads(art["images"] or "[]")
        art["videos"] = json.loads(art["videos"] or "[]")
        art["attachments"] = json.loads(art["attachments"] or "[]")
        art["keywords"] = json.loads(art["keywords"] or "[]")
        meta = json.loads(art["metadata"] or "{}")
        art["metadata"] = meta
        if isinstance(meta, dict):
            for k, v in meta.items():
                art.setdefault(k, v)
        # Rename pubDate to publish_date for frontend compatibility
        art["publish_date"] = art.get("pubDate", "")
        articles.append(art)
    return articles

def save_article(art):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    plan_id = art.get("planId") or art.get("plan_id")
    
    cursor.execute("""
    INSERT OR REPLACE INTO articles (
        id, plan_id, url, title, subtitle, pubDate, modifiedDate, author, category,
        tags, summary, content, images, videos, attachments, language, keywords,
        canonicalUrl, metaDescription, metadata, createdAt
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        art.get("id"),
        plan_id,
        art.get("url"),
        art.get("title"),
        art.get("subtitle"),
        art.get("pubDate"),
        art.get("modifiedDate"),
        art.get("author"),
        art.get("category"),
        json.dumps(art.get("tags", [])),
        art.get("summary"),
        art.get("content"),
        json.dumps(art.get("images", [])),
        json.dumps(art.get("videos", [])),
        json.dumps(art.get("attachments", [])),
        art.get("language"),
        json.dumps(art.get("keywords", [])),
        art.get("canonicalUrl"),
        art.get("metaDescription"),
        json.dumps(art.get("metadata", {})),
        art.get("createdAt")
    ))
    cursor.execute("UPDATE plans SET articlesCount = (SELECT COUNT(*) FROM articles WHERE plan_id = ?) WHERE id = ?", (plan_id, plan_id))
    conn.commit()
    conn.close()

def delete_articles(plan_id=None, article_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if plan_id:
        cursor.execute("DELETE FROM articles WHERE plan_id = ?", (plan_id,))
        cursor.execute("DELETE FROM seen_urls WHERE plan_id = ?", (plan_id,))
        cursor.execute("UPDATE plans SET articlesCount = 0 WHERE id = ?", (plan_id,))
    elif article_id:
        cursor.execute("SELECT plan_id, url FROM articles WHERE id = ?", (article_id,))
        row = cursor.fetchone()
        if row:
            p_id = row["plan_id"]
            url = row["url"]
            cursor.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            cursor.execute("DELETE FROM seen_urls WHERE plan_id = ? AND url = ?", (p_id, url))
            cursor.execute("UPDATE plans SET articlesCount = (SELECT COUNT(*) FROM articles WHERE plan_id = ?) WHERE id = ?", (p_id, p_id))
    else:
        cursor.execute("DELETE FROM articles")
        cursor.execute("DELETE FROM seen_urls")
        cursor.execute("UPDATE plans SET articlesCount = 0")
    conn.commit()
    conn.close()

def get_email_logs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_log ORDER BY ts DESC")
    rows = cursor.fetchall()
    conn.close()
    
    logs = []
    for r in rows:
        el = dict(r)
        el["planId"] = el["plan_id"]
        el["planName"] = el["plan_name"]
        el["articlesCount"] = el["articles_count"]
        logs.append(el)
    return logs

def save_email_log(el):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO email_log (id, plan_id, plan_name, ts, recipient, subject, articles_count, status, error)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        el.get("id"),
        el.get("planId") or el.get("plan_id"),
        el.get("planName") or el.get("plan_name"),
        el.get("ts"),
        el.get("recipient"),
        el.get("subject"),
        el.get("articlesCount") or el.get("articles_count", 0),
        el.get("status"),
        el.get("error")
    ))
    conn.commit()
    conn.close()

def get_logs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM activity_log ORDER BY ts DESC LIMIT 1000")
    rows = cursor.fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        d["plan"] = d.get("planName", "")
        res.append(d)
    return res

def save_log(event, plan_name="", log_type="info"):
    conn = get_db_connection()
    cursor = conn.cursor()
    log_id = f"log_{int(datetime.utcnow().timestamp() * 1000)}"
    cursor.execute("""
    INSERT INTO activity_log (id, ts, event, planName, type)
    VALUES (?, ?, ?, ?, ?)
    """, (
        log_id,
        datetime.utcnow().isoformat() + "Z",
        event,
        plan_name,
        log_type
    ))
    conn.commit()
    conn.close()

def clear_all_data():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plans")
    cursor.execute("DELETE FROM articles")
    cursor.execute("DELETE FROM email_log")
    cursor.execute("DELETE FROM activity_log")
    cursor.execute("DELETE FROM seen_urls")
    conn.commit()
    conn.close()

def clear_activity_logs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM activity_log")
    conn.commit()
    conn.close()

def is_seen_url(plan_id, url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_urls WHERE plan_id = ? AND url = ?", (plan_id, url))
    seen = cursor.fetchone() is not None
    conn.close()
    return seen

def add_seen_url(plan_id, url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_urls (plan_id, url, crawled_at) VALUES (?, ?, ?)",
                   (plan_id, url, datetime.utcnow().isoformat() + "Z"))
    conn.commit()
    conn.close()
