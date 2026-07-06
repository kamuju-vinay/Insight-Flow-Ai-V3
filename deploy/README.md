# Deploying Insight Flow AI to Railway

No app code was changed — only these deployment files were added:
- `Dockerfile` (repo root) — builds the frontend, then runs the backend
- `deploy/entrypoint.sh` — points crawler.db at Railway's persistent Volume
- `railway.json` (repo root) — tells Railway to build with the Dockerfile
- `.dockerignore` (repo root) — keeps node_modules/.venv/etc out of the build

## One-time setup

1. **New Project** on railway.com → **Deploy from GitHub repo** → pick
   `INSIGHT_FLOW_AI_V2` (authorize Railway's GitHub App if it's your first
   time). Railway detects `railway.json` and `Dockerfile` automatically.

2. **Add a Volume** (so crawler.db survives redeploys):
   - Service → Settings → Volumes → **New Volume**
   - Mount path: `/data`
   - This makes `/data` persistent; `entrypoint.sh` symlinks `crawler.db`
     there automatically — no code changes needed.

3. **Environment Variables** (Service → Variables):
   ```
   RESEND_API_KEY=re_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   RESEND_FROM_EMAIL=alerts@yourdomain.com
   ALLOWED_ORIGIN=https://<your-service>.up.railway.app
   ```
   (Leave `PORT` alone — Railway injects it automatically and
   `backend/main.py` already reads `os.environ.get("PORT", 3001)`.)

   Fill in `ALLOWED_ORIGIN` with the actual domain Railway gives you after
   the first deploy (Settings → Networking → Generate Domain), then redeploy
   once so CORS matches.

4. **Deploy.** Railway builds the Docker image and starts the container.
   Watch the build logs; first build takes a few minutes (npm install +
   pip install). Once it's live, hitting the generated `.up.railway.app`
   URL should load the app.

## Ongoing deploys
Every `git push` to `main` triggers an automatic rebuild + redeploy — no
GitHub Actions or SSH keys needed, Railway's GitHub integration handles it.

## Confirming 24/7 behavior
Railway services run continuously by default and do **not** sleep unless
you explicitly enable "Serverless" mode under Settings → Deploy — leave
that off. Check Service → Settings → Deploy to confirm it's not enabled.

## Notes
- Railway's Hobby plan ($5/mo minimum usage) is the right tier for an
  always-on service; the free trial is credit-limited and meant for
  short-term testing only.
- If you ever see the SQLite file reset to empty after a redeploy, it means
  the Volume mount path doesn't match `DATA_DIR` — double check it's `/data`
  in both the Volume settings and (if you changed it) the `DATA_DIR`
  environment variable.
