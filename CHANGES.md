# What changed in this build

## 1. Prompt area enable / disable toggle
- New switch at the top of the **Prompt** tab (per plan): "Prompt Filtering".
- When **ON** (default): behaves exactly as before — the Classification &
  Summary Prompt, the relevance threshold slider, and the Quick Templates
  are editable, and the backend scores every crawled article against the
  prompt (`backend/crawler.py`), keeping only articles at/above the
  threshold.
- When **OFF**: the whole prompt area (textarea, slider, templates, Edit/Save
  buttons) is visually greyed out and locked, and the backend skips relevance
  filtering entirely for that plan — every crawled article that passes the
  date-window filter is saved, unfiltered.
- Persisted as `plan.promptEnabled` (new SQLite column `promptEnabled` on the
  `plans` table, migrated automatically for existing databases via
  `ALTER TABLE ... ADD COLUMN`, default `1`/true so existing plans keep their
  current behavior).

Frontend: `src/App.jsx` — search for `isPromptEnabled` / `togglePromptEnabled`.
Backend: `backend/crawler.py` — search for `prompt_enabled`.
DB: `backend/db.py` — `promptEnabled` column + migration in `init_db()`.

## 2. Case 3 (BFS crawl) fallback ported from `insight_flow_main_logic.ipynb`
The backend already implemented the notebook's Case 1 (structured XML
sitemap) and Case 2/RSS pipelines, but when a site had **neither** a sitemap
**nor** an RSS/Atom feed, it just logged an error and gave up.

Added `backend/bfs_crawler.py`, a port of the notebook's Cell 9 (`run_case3`):
a breadth-first crawl rooted at the plan's URL, depth 2 by default
(depth 0 = seed page, depth 1 = its links, depth 2 = their links), same-domain
only, skipping non-page extensions and low-value paths (tags, categories,
login, search, pagination, etc.). The discovered page URLs are then handed to
the existing `async_engine.crawl_articles()` — the same fetch → classify →
date-extract → summarize pipeline already used by the sitemap and RSS cases —
so article quality/format stays identical across all three cases.

Wired into `backend/crawler.py`: `run_crawl_backend()` now falls through to
`discover_bfs_candidates()` when `find_working_feed()` returns nothing,
instead of returning early.

## Notes
- `node_modules/` and `dist/` were removed from this zip to keep the download
  small — run `npm install` then `npm run build` (or `npm run dev`) to
  regenerate them.
- `crawler.db` (your existing crawled data) was **not** included; a fresh one
  will be created automatically on first run, and `promptEnabled` will
  default to enabled for all existing plans once you re-import `db.json` (if
  you use that legacy migration path) or recreate them.
