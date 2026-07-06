# ============================================================================
# Dockerfile for Insight Flow AI — builds the React frontend, then runs the
# FastAPI backend (which serves that built frontend as static files, exactly
# as backend/main.py already does). No application code is modified.
# ============================================================================

# ---- Stage 1: build the Vite/React frontend -------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
COPY public ./public
RUN npm run build
# Output lands in /app/dist

# ---- Stage 2: Python runtime ------------------------------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

# System deps some Python packages (lxml, pandas) need to build/run
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY backend ./backend
COPY public ./public
COPY --from=frontend-build /app/dist ./dist

# Entrypoint handles the persistent-volume symlink for crawler.db, then
# starts uvicorn exactly like `python -m backend.main` would.
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Railway injects $PORT at runtime; backend/main.py already reads it.
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
