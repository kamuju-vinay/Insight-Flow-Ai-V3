import os
import re
import json
import logging
import sys
import io
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

# Override stdout and stderr encoding to support emojis on Windows, and force
# line-buffering so print() output reaches Railway's log collector immediately
# instead of sitting in an internal buffer.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True, write_through=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True, write_through=True)


from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import httpx
import pandas as pd
import io

# Import database, crawler and scheduler modules
from backend.db import (
    init_db, get_settings, save_settings, get_plans, get_plan, save_plan, delete_plan,
    get_articles, save_article, delete_articles, get_email_logs, save_email_log, get_logs,
    save_log, clear_all_data, is_seen_url,
    count_users, get_user_by_email, get_user_by_id, create_user, claim_orphan_data,
)
from backend.auth import hash_password, verify_password, create_token, get_current_user_id
from backend.crawler import run_crawl_backend, fetch_url_html, is_allowed_by_robots
from backend.scheduler import start_scheduler, stop_scheduler, register_plan_job, remove_plan_job
from backend.email_service import send_html_email, send_notification, get_brevo_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    print("🚀 FastAPI App starting...")
    init_db()
    start_scheduler()
    print("✅ Startup initialization complete!")

    yield

    # ── shutdown ──
    print("🛑 FastAPI App shutting down...")
    stop_scheduler()
    print("✅ Shutdown complete!")


app = FastAPI(title="Insight Flow AI Crawler API", lifespan=lifespan)

# Configure CORS
allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5175",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
allowed_origin_env = os.environ.get("ALLOWED_ORIGIN")
if allowed_origin_env:
    allowed_origins.append(allowed_origin_env)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket Manager ────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/api/ws/crawl")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ── Auth Endpoints ───────────────────────────────────────
# NOTE: login/signup intentionally return normal 200 responses with an
# "error" field on failure (never a 401) — the frontend's apiFetch treats
# any 401 as "your session expired" and force-reloads to the login screen,
# which would make a wrong password look like a broken app instead of a
# clear "wrong password" message.

@app.post("/api/auth/signup")
def signup(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or "@" not in email:
        return {"error": "Enter a valid email address."}
    if len(password) < 8:
        return {"error": "Password must be at least 8 characters."}
    if get_user_by_email(email):
        return {"error": "An account with that email already exists."}

    is_first_user = count_users() == 0
    user = create_user(email, hash_password(password))
    if is_first_user:
        # One-time convenience: claim any plans/articles/logs that existed
        # before accounts existed, so nothing appears to "disappear".
        try:
            claim_orphan_data(user["id"])
        except Exception as e:
            print(f"⚠️ Could not claim orphaned data for first user: {e}")

    token = create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


@app.post("/api/auth/login")
def login(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return {"error": "Incorrect email or password."}

    token = create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


@app.get("/api/auth/me")
def get_me(user_id: str = Depends(get_current_user_id)):
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"user": {"id": user["id"], "email": user["email"]}}

# ── Settings Endpoints ───────────────────────────────────
# Settings (SMTP, AI keys, crawler config) stay shared team-wide by design —
# only plans/articles are private per-user. But they must require login,
# since they contain secrets (SMTP password, AI API keys).
@app.get("/api/settings")
def get_api_settings(user_id: str = Depends(get_current_user_id)):
    return get_settings()

@app.put("/api/settings")
def update_api_settings(payload: dict, user_id: str = Depends(get_current_user_id)):
    save_settings(payload)
    return get_settings()

# ── Plans Endpoints ──────────────────────────────────────
@app.get("/api/plans")
def get_api_plans(user_id: str = Depends(get_current_user_id)):
    return get_plans(user_id=user_id)

@app.post("/api/plans")
def create_api_plan(payload: dict, user_id: str = Depends(get_current_user_id)):
    payload["user_id"] = user_id
    save_plan(payload)
    register_plan_job(payload)
    return payload

@app.put("/api/plans/{plan_id}")
def update_api_plan(plan_id: str, payload: dict, user_id: str = Depends(get_current_user_id)):
    existing = get_plan(plan_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Plan not found")
    payload["user_id"] = user_id
    save_plan(payload)
    register_plan_job(payload)
    return payload

@app.delete("/api/plans/{plan_id}")
def delete_api_plan(plan_id: str, user_id: str = Depends(get_current_user_id)):
    existing = get_plan(plan_id)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Plan not found")
    remove_plan_job(plan_id)
    delete_plan(plan_id)
    return {"success": True}

# Trigger Crawl Task
def run_crawl_task(plan_id: str):
    try:
        run_crawl_backend(plan_id)
    except Exception as e:
        logging.error("Crawl task error for plan %s: %s", plan_id, e)
        # Mark plan as idle/failed
        plan = get_plan(plan_id)
        if plan:
            plan["status"] = "running"
            plan["stage"] = "done"
            save_plan(plan)
            save_log(event=f"Crawl failed: {str(e)}", plan_name=plan.get("name", plan_id), log_type="error", plan_id=plan_id)

@app.post("/api/plans/{plan_id}/run")
def run_api_plan(plan_id: str, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user_id)):
    plan = get_plan(plan_id)
    if not plan or plan.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan.get("stage") in ["crawling", "analyzing", "summarizing", "sending"]:
        return {"error": "Crawl already running for this plan"}

    background_tasks.add_task(run_crawl_task, plan_id)
    return {"success": True}

# ── Articles Endpoints ───────────────────────────────────
@app.get("/api/articles")
def get_api_articles(planId: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    if planId:
        plan = get_plan(planId)
        if not plan or plan.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Plan not found")
    return get_articles(planId, user_id=user_id)

@app.post("/api/articles")
def create_api_article(payload: dict, user_id: str = Depends(get_current_user_id)):
    plan_id = payload.get("planId") or payload.get("plan_id")
    if plan_id:
        plan = get_plan(plan_id)
        if not plan or plan.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Plan not found")
    payload["user_id"] = user_id
    save_article(payload)
    return payload

@app.delete("/api/articles")
def delete_api_articles(planId: Optional[str] = None, id: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    if planId:
        plan = get_plan(planId)
        if not plan or plan.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Plan not found")
    delete_articles(planId, id)
    return {"success": True}

# ── Logs & Email Logs Endpoints ─────────────────────────
@app.get("/api/email-log")
def get_api_email_logs(user_id: str = Depends(get_current_user_id)):
    return get_email_logs(user_id=user_id)

@app.get("/api/logs")
def get_api_logs(user_id: str = Depends(get_current_user_id)):
    return get_logs(user_id=user_id)

@app.post("/api/logs")
def create_api_log(payload: dict, user_id: str = Depends(get_current_user_id)):
    save_log(
        payload.get("event", ""),
        payload.get("planName", ""),
        payload.get("type", "info"),
        user_id=user_id,
    )
    return {"success": True}

# ── Mail Sender (Brevo REST API only — SMTP/Resend are not used) ──────────
@app.post("/api/send-email")
def send_email_endpoint(payload: dict, user_id: str = Depends(get_current_user_id)):
    """General-purpose send used by the frontend's manual "Send Now" flow
    and by the standalone email test page. Supports cc/bcc/attachments so
    it doubles as the endpoint behind the Settings → Test Email UI."""
    to = payload.get("to")
    subject = payload.get("subject")
    html = payload.get("html")
    plan_id = payload.get("plan_id")

    if not all([to, subject, html]):
        raise HTTPException(status_code=400, detail="Missing recipient, subject, or email content.")

    if plan_id:
        plan = get_plan(plan_id)
        if not plan or plan.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Plan not found")

    result = send_html_email(
        to, subject, html,
        cc=payload.get("cc"),
        bcc=payload.get("bcc"),
        attachments=payload.get("attachments"),
        articles_count=payload.get("articles_count", 0),
        plan_id=plan_id,
        plan_name="Manual Digest" if not plan_id else "",
    )

    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Email send failed: {result['error']}")
    return {"success": True, "messageId": result["messageId"], "status": "Email Sent"}


@app.get("/api/email-config-status")
def email_config_status():
    """Lets the frontend show a clear "Brevo is/isn't configured" banner
    without exposing the actual API key."""
    cfg = get_brevo_config()
    return {
        "configured": bool(cfg["api_key"] and cfg["from_email"]),
        "fromEmail": cfg["from_email"],
        "fromName": cfg["from_name"],
    }


@app.post("/api/test-email")
def test_email_endpoint(payload: dict = None, user_id: str = Depends(get_current_user_id)):
    """Sends a sample email to verify the Brevo integration end-to-end.
    Body: { "to": "someone@example.com" } — subject/body are fixed so this
    stays a pure connectivity/deliverability check."""
    payload = payload or {}
    to = payload.get("to")
    if not to:
        raise HTTPException(status_code=400, detail="Missing recipient email address (\"to\").")

    result = send_notification(
        to,
        subject="Insight Flow AI — Test Email",
        message=(
            "This is a test email from Insight Flow AI, sent via the Brevo REST API. "
            "If you received this, your Brevo integration on Railway is working correctly."
        ),
    )

    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Test email failed: {result['error']}")
    return {"success": True, "messageId": result["messageId"], "status": "Email Sent"}

# ── AI Proxy Call ────────────────────────────────────────
@app.post("/api/call-ai")
async def call_ai_proxy(payload: dict):
    provider = payload.get("provider")
    apiKey = payload.get("apiKey")
    systemPrompt = payload.get("systemPrompt", "")
    userPrompt = payload.get("userPrompt", "")
    model = payload.get("model")

    if not provider or not apiKey:
        raise HTTPException(status_code=400, detail="Missing AI provider or API key.")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            if provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or 'gemini-2.0-flash'}:generateContent?key={apiKey}"
                resp = await client.post(url, json={
                    "systemInstruction": {"parts": [{"text": systemPrompt}]},
                    "contents": [{"role": "user", "parts": [{"text": userPrompt}]}]
                })
                data = resp.json()
                if "error" in data:
                    raise HTTPException(status_code=resp.status_code, detail=data["error"].get("message", "Gemini API Error"))
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return {"text": text, "quota": {"remainingRequests": "N/A", "remainingTokens": "N/A"}}

            elif provider == "huggingface":
                url = "https://router.huggingface.co/v1/chat/completions"
                resp = await client.post(url, headers={"Authorization": f"Bearer {apiKey}"}, json={
                    "model": model or "meta-llama/Llama-3.2-3B-Instruct",
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": systemPrompt},
                        {"role": "user", "content": userPrompt}
                    ]
                })
                data = resp.json()
                if "error" in data or resp.status_code != 200:
                    err_msg = data.get("error", {}).get("message") or data.get("error") or "Hugging Face Error"
                    raise HTTPException(status_code=resp.status_code, detail=err_msg)
                text = data["choices"][0]["message"]["content"]
                return {"text": text, "quota": {"remainingRequests": "N/A", "remainingTokens": "N/A"}}

            elif provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
                resp = await client.post(url, headers={"Authorization": f"Bearer {apiKey}"}, json={
                    "model": model or "gpt-4o-mini",
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": systemPrompt},
                        {"role": "user", "content": userPrompt}
                    ]
                })
                data = resp.json()
                if "error" in data or resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=data["error"].get("message", "OpenAI Error"))
                text = data["choices"][0]["message"]["content"]
                return {"text": text, "quota": {"remainingRequests": "N/A", "remainingTokens": "N/A"}}

            elif provider == "groq":
                url = "https://api.groq.com/openai/v1/chat/completions"
                resp = await client.post(url, headers={"Authorization": f"Bearer {apiKey}"}, json={
                    "model": model or "llama-3.3-70b-versatile",
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": systemPrompt},
                        {"role": "user", "content": userPrompt}
                    ]
                })
                data = resp.json()
                if "error" in data or resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=data["error"].get("message", "Groq Error"))
                text = data["choices"][0]["message"]["content"]
                return {"text": text, "quota": {"remainingRequests": "N/A", "remainingTokens": "N/A"}}

            elif provider == "claude":
                url = "https://api.anthropic.com/v1/messages"
                resp = await client.post(url, headers={
                    "x-api-key": apiKey,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                }, json={
                    "model": model or "claude-3-5-sonnet-20241022",
                    "max_tokens": 1000,
                    "system": systemPrompt,
                    "messages": [{"role": "user", "content": userPrompt}]
                })
                data = resp.json()
                if "error" in data or resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=data["error"].get("message", "Claude Error"))
                text = data["content"][0]["text"]
                return {"text": text, "quota": {"remainingRequests": "N/A", "remainingTokens": "N/A"}}
            else:
                raise HTTPException(status_code=400, detail="Unsupported AI provider.")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

# ── Validate Key Endpoints ───────────────────────────────
@app.post("/api/validate-key")
async def validate_key(payload: dict):
    provider = payload.get("provider")
    apiKey = payload.get("apiKey")

    if not provider or not apiKey:
        raise HTTPException(status_code=400, detail="Missing AI provider or API key.")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={apiKey}"
                resp = await client.post(url, json={
                    "contents": [{"role": "user", "parts": [{"text": "ping"}]}]
                })
                data = resp.json()
                if "error" in data:
                    return {"success": False, "message": data["error"].get("message")}
                return {"success": True}

            elif provider == "huggingface":
                resp = await client.get("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {apiKey}"})
                if resp.status_code != 200:
                    return {"success": False, "message": "Invalid token"}
                return {"success": True}

            elif provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
                resp = await client.post(url, headers={"Authorization": f"Bearer {apiKey}"}, json={
                    "model": "gpt-4o-mini",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}]
                })
                if resp.status_code != 200:
                    return {"success": False, "message": "Invalid API key"}
                return {"success": True}

            elif provider == "groq":
                url = "https://api.groq.com/openai/v1/chat/completions"
                resp = await client.post(url, headers={"Authorization": f"Bearer {apiKey}"}, json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}]
                })
                if resp.status_code != 200:
                    return {"success": False, "message": "Invalid API key"}
                return {"success": True}

            elif provider == "claude":
                url = "https://api.anthropic.com/v1/messages"
                resp = await client.post(url, headers={
                    "x-api-key": apiKey,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                }, json={
                    "model": "claude-3-5-haiku-20241022",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}]
                })
                if resp.status_code != 200:
                    return {"success": False, "message": "Invalid API key"}
                return {"success": True}
            else:
                return {"success": False, "message": "Unsupported provider"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ── SSRF & Fetch URL Proxy ───────────────────────────────
def is_private_ip(ip: str) -> bool:
    if re.match(r"^(127\.|10\.|192\.168\.)", ip) or re.match(r"^172\.(1[6-9]|2[0-9]|3[0-1])\.", ip) or re.match(r"^169\.254\.", ip):
        return True
    if ip == "::1" or ip.startswith("fe80:") or ip.lower().startswith("fc") or ip.lower().startswith("fd"):
        return True
    return False

@app.get("/api/fetch-url")
def fetch_url(url: str = Query(...)):
    # SSRF checking
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https"]:
        raise HTTPException(status_code=400, detail="Invalid scheme. Only HTTP and HTTPS are allowed.")
    if is_private_ip(parsed.hostname or ""):
        raise HTTPException(status_code=403, detail="SSRF Protection triggered. Private IPs are forbidden.")

    # robots.txt check
    allowed = is_allowed_by_robots(url)
    if not allowed:
        raise HTTPException(status_code=403, detail="Fetching this page is disallowed by robots.txt")

    resp_html, resp_headers = fetch_url_html(url)
    if not resp_html:
        raise HTTPException(status_code=502, detail="Fetch failed: could not download the page")
    return resp_html

# ── Export Endpoint ──────────────────────────────────────
@app.get("/api/export")
def export_articles(planId: Optional[str] = None, format: str = Query("csv"), user_id: str = Depends(get_current_user_id)):
    if planId:
        plan = get_plan(planId)
        if not plan or plan.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Plan not found")
    articles = get_articles(planId, user_id=user_id)
    if not articles:
        raise HTTPException(status_code=404, detail="No articles found to export")

    # Construct DataFrame
    df = pd.DataFrame(articles)
    
    # Flatten/clean columns that are lists
    for col in ["tags", "images", "videos", "attachments", "keywords", "metadata"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: ", ".join(x) if isinstance(x, list) else str(x))

    if format.lower() == "csv":
        stream = io.StringIO()
        df.to_csv(stream, index=False)
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename=articles_{planId or 'all'}.csv"
        return response
    elif format.lower() == "excel":
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Articles")
        output.seek(0)
        headers = {
            'Content-Disposition': f'attachment; filename="articles_{planId or "all"}.xlsx"'
        }
        return StreamingResponse(output, headers=headers, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:  # JSON
        stream = io.StringIO()
        df.to_json(stream, orient="records", indent=2)
        response = StreamingResponse(iter([stream.getvalue()]), media_type="application/json")
        response.headers["Content-Disposition"] = f"attachment; filename=articles_{planId or 'all'}.json"
        return response

# ── Clear All Data ───────────────────────────────────────
@app.delete("/api/data")
def clear_data(user_id: str = Depends(get_current_user_id)):
    clear_all_data(user_id=user_id)
    return {"success": True}

# ── Clear Activity Logs ──────────────────────────────────
@app.delete("/api/logs")
def clear_api_logs(user_id: str = Depends(get_current_user_id)):
    from backend.db import clear_activity_logs
    clear_activity_logs(user_id=user_id)
    return {"success": True}

# ── Mount static SPA React frontend ──────────────────────
dist_path = os.path.join(os.path.dirname(__file__), "..", "dist")

if os.path.exists(dist_path):
    app.mount("/", StaticFiles(directory=dist_path, html=True), name="static")
else:
    print(f"⚠️ Warning: Static files directory '{dist_path}' not found. Make sure to build React front-end (npm run build).")
    
# SPA catch-all
@app.exception_handler(404)
def catch_all_redirect_to_index(request, exc):
    index_file = os.path.join(dist_path, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse(status_code=404, content={"error": "Not Found"})

if __name__ == "__main__":
    import uvicorn
    # Render (and most PaaS hosts) inject PORT and expect the app to bind 0.0.0.0.
    # Locally this still defaults to 127.0.0.1:3001 with autoreload for dev.
    port = int(os.environ.get("PORT", 3001))
    is_prod = "PORT" in os.environ  # Render always sets PORT
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0" if is_prod else "127.0.0.1",
        port=port,
        reload=not is_prod,
    )
