import os
import re
import json
import logging
import smtplib
import sys
import io
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

# Override stdout and stderr encoding to support emojis on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
import pandas as pd
import io

# Import database, crawler and scheduler modules
from backend.db import (
    init_db, get_settings, save_settings, get_plans, get_plan, save_plan, delete_plan,
    get_articles, save_article, delete_articles, get_email_logs, save_email_log, get_logs,
    save_log, clear_all_data, is_seen_url
)
from backend.crawler import run_crawl_backend, fetch_url_html, is_allowed_by_robots
from backend.scheduler import start_scheduler, stop_scheduler, register_plan_job, remove_plan_job

app = FastAPI(title="Insight Flow AI Crawler API")

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

# ── Startup and Shutdown Hooks ──────────────────────────
@app.on_event("startup")
def startup_event():
    print("🚀 FastAPI App starting...")
    init_db()
    start_scheduler()
    print("✅ Startup initialization complete!")

@app.on_event("shutdown")
def shutdown_event():
    print("🛑 FastAPI App shutting down...")
    stop_scheduler()
    print("✅ Shutdown complete!")

# ── Auth Endpoints ───────────────────────────────────────
@app.post("/api/auth/login")
def login(payload: dict):
    email = payload.get("email", "user@example.com")
    return {"token": "mock-jwt-token", "user": {"email": email}}

@app.post("/api/auth/signup")
def signup(payload: dict):
    email = payload.get("email", "user@example.com")
    return {"token": "mock-jwt-token", "user": {"email": email}}

@app.get("/api/auth/me")
def get_me():
    return {"user": {"email": "user@example.com"}}

# ── Settings Endpoints ───────────────────────────────────
@app.get("/api/settings")
def get_api_settings():
    return get_settings()

@app.put("/api/settings")
def update_api_settings(payload: dict):
    save_settings(payload)
    return get_settings()

# ── Plans Endpoints ──────────────────────────────────────
@app.get("/api/plans")
def get_api_plans():
    return get_plans()

@app.post("/api/plans")
def create_api_plan(payload: dict):
    save_plan(payload)
    register_plan_job(payload)
    return payload

@app.put("/api/plans/{plan_id}")
def update_api_plan(plan_id: str, payload: dict):
    save_plan(payload)
    register_plan_job(payload)
    return payload

@app.delete("/api/plans/{plan_id}")
def delete_api_plan(plan_id: str):
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
            save_log(event=f"Crawl failed: {str(e)}", plan_name=plan.get("name", plan_id), log_type="error")

@app.post("/api/plans/{plan_id}/run")
def run_api_plan(plan_id: str, background_tasks: BackgroundTasks):
    plan = get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
        
    if plan.get("stage") in ["crawling", "analyzing", "summarizing", "sending"]:
        return {"error": "Crawl already running for this plan"}
        
    background_tasks.add_task(run_crawl_task, plan_id)
    return {"success": True}

# ── Articles Endpoints ───────────────────────────────────
@app.get("/api/articles")
def get_api_articles(planId: Optional[str] = None):
    return get_articles(planId)

@app.post("/api/articles")
def create_api_article(payload: dict):
    save_article(payload)
    return payload

@app.delete("/api/articles")
def delete_api_articles(planId: Optional[str] = None, id: Optional[str] = None):
    delete_articles(planId, id)
    return {"success": True}

# ── Logs & Email Logs Endpoints ─────────────────────────
@app.get("/api/email-log")
def get_api_email_logs():
    return get_email_logs()

@app.get("/api/logs")
def get_api_logs():
    return get_logs()

@app.post("/api/logs")
def create_api_log(payload: dict):
    save_log(
        payload.get("event", ""),
        payload.get("planName", ""),
        payload.get("type", "info")
    )
    return {"success": True}

# ── SMTP Mail Sender Proxy ──────────────────────────────
@app.post("/api/send-email")
def send_email_smtp(payload: dict):
    smtp_host = payload.get("smtpHost")
    smtp_port = payload.get("smtpPort")
    smtp_user = payload.get("smtpUser")
    smtp_password = payload.get("smtpPassword")
    sender_email = payload.get("senderEmail") or smtp_user
    sender_name = payload.get("senderName") or "Insight Flow AI"
    to = payload.get("to")
    subject = payload.get("subject")
    html = payload.get("html")
    plan_id = payload.get("plan_id")

    if not all([smtp_host, smtp_port, smtp_user, smtp_password, to, subject, html]):
        raise HTTPException(status_code=400, detail="Missing SMTP parameters or email content.")

    log_id = f"el_{int(datetime.utcnow().timestamp() * 1000)}"
    email_log_payload = {
        "id": log_id,
        "planId": plan_id,
        "planName": "Manual Digest" if not plan_id else "",
        "ts": datetime.utcnow().isoformat() + "Z",
        "recipient": to,
        "subject": subject,
        "articlesCount": payload.get("articles_count", 0),
        "status": "pending",
        "error": ""
    }

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f'"{sender_name}" <{sender_email}>'
        msg["To"] = to

        part = MIMEText(html, "html", "utf-8")
        msg.attach(part)

        port = int(smtp_port)
        is_ssl = port == 465

        if is_ssl:
            server = smtplib.SMTP_SSL(smtp_host, port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, port, timeout=30)
            server.starttls()

        server.login(smtp_user, smtp_password)
        server.sendmail(sender_email, [to], msg.as_string())
        server.quit()

        email_log_payload["status"] = "sent"
        save_email_log(email_log_payload)
        return {"success": True}
    except Exception as e:
        email_log_payload["status"] = "failed"
        email_log_payload["error"] = str(e)
        save_email_log(email_log_payload)
        raise HTTPException(status_code=500, detail=f"SMTP Send Error: {str(e)}")

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
def export_articles(planId: Optional[str] = None, format: str = Query("csv")):
    articles = get_articles(planId)
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
def clear_data():
    clear_all_data()
    return {"success": True}

# ── Clear Activity Logs ──────────────────────────────────
@app.delete("/api/logs")
def clear_api_logs():
    from backend.db import clear_activity_logs
    clear_activity_logs()
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
