-- =============================================================================
-- Insight Flow AI — Supabase schema
-- Run this ONCE in Supabase Dashboard > SQL Editor > New query > Run
-- Safe to re-run (uses IF NOT EXISTS everywhere).
-- =============================================================================

create table if not exists settings (
    key   text primary key,
    value text
);

create table if not exists plans (
    id                    text primary key,
    name                  text,
    icon                  text,
    bg                    text,
    status                text,
    stage                 text default 'idle',
    urls                  jsonb default '[]',
    recipient_groups      jsonb default '[]',
    periods               jsonb default '[]',
    trigger_times         jsonb default '{}',
    prompt                text,
    keywords              text,
    articles_count        integer default 0,
    emails_count          integer default 0,
    last_run              text,
    created_at            text,
    continuous_run        boolean default false,
    relevance_threshold   integer default 70,
    search_body_keywords  boolean default false,
    enable_ai_keywords    boolean default true,
    crawl_state           jsonb default '{}',
    fetch_period          text default 'week',
    fetch_period_days     integer default 7,
    prompt_enabled        boolean default true,
    sched_freq            text,
    sched_time            text,
    sched_week_days       jsonb default '[]',
    sched_month_day       integer,
    interval_minutes      integer,
    sched_custom_unit     text,
    sched_tz              text,
    auto_mail             boolean default false,
    send_mode             text,
    send_time             text
);

create table if not exists articles (
    id               text primary key,
    plan_id          text references plans(id) on delete cascade,
    url              text,
    title            text,
    subtitle         text,
    pub_date         text,
    modified_date    text,
    author           text,
    category         text,
    tags             jsonb default '[]',
    summary          text,
    content          text,
    images           jsonb default '[]',
    videos           jsonb default '[]',
    attachments      jsonb default '[]',
    language         text,
    keywords         jsonb default '[]',
    canonical_url    text,
    meta_description text,
    metadata         jsonb default '{}',
    created_at       text
);
create index if not exists idx_articles_plan_id on articles(plan_id);

create table if not exists email_log (
    id             text primary key,
    plan_id        text,
    plan_name      text,
    ts             text,
    recipient      text,
    subject        text,
    articles_count integer,
    status         text,
    error          text,
    message_id     text
);

create table if not exists activity_log (
    id        text primary key,
    ts        text,
    event     text,
    plan_name text,
    type      text
);

create table if not exists seen_urls (
    plan_id    text,
    url        text,
    crawled_at text,
    primary key (plan_id, url)
);

-- Sensible starting settings (matches the old sqlite defaults)
insert into settings (key, value) values
    ('respect_robots_txt', 'true'),
    ('concurrent_workers', '20'),
    ('timeout', '10'),
    ('retry_count', '3'),
    ('delay_between_requests', '1'),
    ('headers', '{}'),
    ('proxy', '')
on conflict (key) do nothing;

-- NOTE on Row Level Security:
-- These tables are accessed only from your FastAPI backend using the
-- service_role key, which bypasses RLS entirely. RLS is left disabled here
-- (Supabase default for new tables) since there is no direct client-side
-- access to this database. If you ever expose these tables to the
-- Supabase client from the browser, enable RLS and add policies first.
