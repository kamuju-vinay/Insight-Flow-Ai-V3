-- =============================================================================
-- Insight Flow AI — Multi-user migration
-- Run this ONCE in Supabase Dashboard > SQL Editor > New query > Run
-- (Run AFTER supabase_schema.sql has already been run once.)
-- Safe to re-run.
-- =============================================================================

-- Real user accounts (separate from Supabase's own auth system — this app
-- manages its own login/signup since the backend already had that UI built).
create table if not exists app_users (
    id            text primary key,
    email         text unique not null,
    password_hash text not null,
    created_at    text
);

-- Ownership columns — nullable so this migration is safe to run on a project
-- that already has existing (currently-shared) data in it.
alter table plans        add column if not exists user_id text references app_users(id) on delete cascade;
alter table articles     add column if not exists user_id text;
alter table email_log    add column if not exists user_id text;
alter table activity_log add column if not exists user_id text;

create index if not exists idx_plans_user_id        on plans(user_id);
create index if not exists idx_articles_user_id     on articles(user_id);
create index if not exists idx_email_log_user_id    on email_log(user_id);
create index if not exists idx_activity_log_user_id on activity_log(user_id);

-- NOTE: any plans/articles/logs created before this migration will have
-- user_id = NULL ("orphaned"). The backend automatically assigns all
-- orphaned data to the very first person who signs up after this migration
-- runs — so sign up with your own account first, and your existing plans
-- will show up under your account. Anyone who signs up after that starts
-- with a clean, empty, private workspace.
