"""
Server-side email digest builder + sender.

This is a faithful Python port of `compileEmailHtml` / `sendEmailForPlan`
from src/App.jsx, so the email design is byte-for-byte the same regardless
of whether the send was triggered by the browser or by this backend's
scheduler. Do not change the HTML/CSS below without also updating App.jsx,
or the two will drift apart.

Email provider: Brevo REST API (HTTPS) only. Railway blocks outbound SMTP
ports, so SMTP and Resend are intentionally not used anywhere in this
module. See BREVO_API_KEY / BREVO_FROM_EMAIL / BREVO_FROM_NAME in Railway →
Variables.
"""
import os
import re
import time
import base64
import logging
from datetime import datetime

import httpx

from backend.db import get_settings, save_email_log

log = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

LOGO_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "dist", "logo.png"),
    os.path.join(os.path.dirname(__file__), "..", "public", "logo.png"),
]


def _find_logo():
    for p in LOGO_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ── Small helpers ported from App.jsx ───────────────────────────────────
def escape_html(s):
    if s is None:
        return ""
    s = str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def bold_md(s):
    """Mirrors: text.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")"""
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s or "")


def get_article_category(title, summary):
    text = f"{title or ''} {summary or ''}".lower()
    if re.search(
        r"\b(climate|carbon|emission|emissions|energy|renewable|solar|wind|water|waste|"
        r"sustainability|eco|nature|warming|environment|environmental|green|forest|"
        r"biodiversity|pollution|recycle|recycling|net-zero|net zero|esg|conservation|"
        r"circular economy)\b",
        text,
    ):
        return {"name": "Environmental", "icon": "🌱", "color": "#10b981", "bg": "#ecfdf5"}
    if re.search(
        r"\b(nurse|dies|mishap|accident|community|employee|labor|human rights|safety|health|"
        r"education|diversity|inclusion|social|public|people|society|worker|workers|"
        r"humanitarian|welfare|charity|labor rights|workplace|fair wage|philanthropy)\b",
        text,
    ):
        return {"name": "Social", "icon": "🤝", "color": "#3b82f6", "bg": "#eff6ff"}
    if re.search(
        r"\b(board|executive|audit|ethics|compliance|regulatory|law|policy|governance|"
        r"shareholder|rights|corruption|bribery|tax|management|corporate|sec|ftc|lawsuit|"
        r"fines|prosecute|shareholders|transparency|anti-corruption|insider trading|"
        r"whistleblower)\b",
        text,
    ):
        return {"name": "Governance", "icon": "⚖️", "color": "#8b5cf6", "bg": "#f5f3ff"}
    return {"name": "General", "icon": "📊", "color": "#6b7280", "bg": "#f3f4f6"}


_STYLE_BLOCK = """
    body {
      margin: 0;
      padding: 0;
      background-color: #f8fafc;
      font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif !important;
    }
    @media only screen and (max-width: 600px) {
      .email-container { width: 100% !important; margin-top: 0 !important; margin-bottom: 0 !important; border-radius: 0 !important; border: none !important; }
      .email-content { padding-right: 16px !important; padding-left: 16px !important; }
      .header-left { display: block !important; width: 100% !important; text-align: center !important; padding-bottom: 16px !important; }
      .header-left table { margin: 0 auto !important; }
      .header-right { display: block !important; width: 100% !important; text-align: center !important; }
      .header-right table { margin: 0 auto !important; width: 100% !important; }
      .summary-card { width: 100% !important; }
      .article-btn-col { display: block !important; width: 100% !important; text-align: left !important; padding-left: 60px !important; padding-top: 12px !important; box-sizing: border-box !important; }
      .footer-col { display: inline-block !important; width: 48% !important; margin-bottom: 16px !important; border-right: none !important; vertical-align: top !important; }
    }
"""


def _article_row(a, index):
    title = bold_md(escape_html(a.get("title", "")))
    summary = bold_md(escape_html(a.get("summary", "")))
    cat = get_article_category(a.get("title"), a.get("summary"))
    company = escape_html(a.get("company", ""))
    pub_date = escape_html(a.get("publish_date", "") or a.get("publishedDate", ""))
    url = a.get("url", "")

    insights = a.get("key_insights") or a.get("key_points") or []
    insights_html = ""
    if insights:
        items = "".join(f'<li style="margin-bottom: 3px;">{escape_html(ki)}</li>' for ki in insights)
        insights_html = f"""
                  <div style="font-size: 12px; color: #475569; line-height: 1.5; font-family: 'Inter', sans-serif; margin-bottom: 6px; padding: 8px 10px; background-color: #f8fafc; border-left: 3px solid #2563eb; border-radius: 4px; margin-top: 8px;">
                    <strong style="color: #0f172a; display: block; margin-bottom: 4px;">Key Insights:</strong>
                    <ul style="margin: 0; padding-left: 18px; color: #334155; font-size: 11.5px;">
                      {items}
                    </ul>
                  </div>"""

    return f"""
      <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; margin-bottom: 16px; border-collapse: separate; mso-table-lspace: 0pt; mso-table-rspace: 0pt; box-shadow: 0 1px 3px rgba(0,0,0,0.01);">
        <tr>
          <td style="padding: 16px; font-family: 'Outfit', 'Inter', sans-serif;">
            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td width="48" valign="top" style="padding-right: 12px;">
                  <table cellpadding="0" cellspacing="0" width="44" height="44" style="background-color: {cat['bg']}; border-radius: 10px; text-align: center; border-collapse: separate;">
                    <tr>
                      <td align="center" valign="middle" style="font-family: 'Outfit', 'Inter', sans-serif; font-size: 16px; font-weight: 800; color: {cat['color']}; line-height: 44px;">
                        {index + 1}
                      </td>
                    </tr>
                  </table>
                </td>
                <td class="article-content" valign="top" style="font-family: 'Outfit', 'Inter', sans-serif;">
                  <div style="font-weight: 700; font-size: 14px; line-height: 1.4; margin-bottom: 3px;">
                    <a href="{url}" target="_blank" style="color: #0f172a; text-decoration: none;">{title}</a>
                  </div>
                  <div style="font-size: 11px; color: #64748b; font-weight: 500; margin-bottom: 6px;">
                    {company} &nbsp;•&nbsp; {pub_date}
                  </div>
                  <div style="font-size: 12px; color: #475569; line-height: 1.5; font-family: 'Inter', sans-serif; margin-bottom: 6px;">
                    <strong>AI Summary:</strong> {summary}
                  </div>{insights_html}
                  <div style="font-size: 11px; font-family: 'Inter', sans-serif; word-break: break-all; margin-top: 6px;">
                    <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{url}</a>
                  </div>
                </td>
                <td class="article-btn-col" width="130" align="right" valign="middle" style="padding-left: 12px;">
                  <a href="{url}" target="_blank" style="display: inline-block; background-color: #ffffff; border: 1px solid #cbd5e1; color: #2563eb; padding: 8px 14px; text-decoration: none; font-size: 11px; font-weight: 700; border-radius: 8px; font-family: 'Outfit', 'Inter', sans-serif; white-space: nowrap;">
                    View Full Article &nbsp;<span style="font-size: 12px; line-height: 10px;">&rsaquo;</span>
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    """


def compile_email_html(plan, articles):
    """Python port of compileEmailHtml(plan, articles) from src/App.jsx."""
    plan_name = plan.get("name") or "ESG Application"
    date_str = datetime.now().strftime("%-d %b %Y") if os.name != "nt" else datetime.now().strftime("%#d %b %Y")
    logo_src = "cid:logo.png"
    articles_list_html = "".join(_article_row(a, i) for i, a in enumerate(articles) if a)
    count_label = f"{len(articles)} New Article{'' if len(articles) == 1 else 's'}"
    plan_name_esc = escape_html(plan_name)

    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{plan_name_esc} Digest</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style type="text/css">{_STYLE_BLOCK}</style>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc;">
  <table class="email-container" align="center" border="0" cellpadding="0" cellspacing="0" width="600" style="border-collapse: collapse; background-color: #ffffff; margin-top: 20px; margin-bottom: 20px; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.02);">
    <tr>
      <td class="email-content" style="padding: 24px 24px 20px 24px; border-bottom: 1px solid #f1f5f9;">
        <table cellpadding="0" cellspacing="0" width="100%">
          <tr>
            <td class="header-left" valign="middle">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td valign="middle" style="padding-right: 10px;">
                    <img src="{logo_src}" alt="InsightFlow" style="display: block; border: 0; width: 36px; height: 36px; border-radius: 8px;" width="36" height="36" />
                  </td>
                  <td valign="middle" style="font-family: 'Outfit', 'Inter', sans-serif; font-size: 24px; font-weight: 800; line-height: 1;">
                    <span style="color: #0f172a;">Insight</span><span style="color: #2563eb;">Flow</span> <span style="font-size: 20px; font-weight: 700; color: #4f46e5;">AI</span>
                  </td>
                </tr>
                <tr>
                  <td colspan="2" style="font-size: 11px; color: #64748b; padding-top: 6px; font-family: 'Outfit', 'Inter', sans-serif; font-weight: 600; letter-spacing: 0.02em;">
                    Smart AI Responses. On Schedule. In Your Inbox.
                  </td>
                </tr>
              </table>
            </td>
            <td class="header-right" align="right" valign="middle">
              <table class="summary-card" cellpadding="0" cellspacing="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 12px 16px; width: 230px; border-collapse: separate;">
                <tr>
                  <td valign="middle" style="font-family: 'Outfit', 'Inter', sans-serif;">
                    <div style="font-size: 10px; font-weight: 700; color: #2563eb; text-transform: uppercase; letter-spacing: 0.05em;">{plan_name_esc}</div>
                    <div style="font-size: 14px; font-weight: 800; color: #0f172a; margin-top: 2px;">{count_label}</div>
                    <div style="font-size: 11px; color: #64748b; margin-top: 4px; font-weight: 500;">📅 {date_str}</div>
                  </td>
                  <td width="10">&nbsp;</td>
                  <td width="60" valign="middle" align="right">
                    <table cellpadding="0" cellspacing="0" style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px; width: 60px; height: 50px; text-align: center; border-collapse: separate;">
                      <tr>
                        <td valign="bottom" align="center" style="height: 34px; padding-bottom: 2px;">
                          <div style="display: inline-block; width: 6px; height: 16px; background-color: #cbd5e1; border-radius: 2px; margin-right: 2px;"></div>
                          <div style="display: inline-block; width: 6px; height: 24px; background-color: #6366f1; border-radius: 2px; margin-right: 2px;"></div>
                          <div style="display: inline-block; width: 6px; height: 32px; background-color: #4f46e5; border-radius: 2px;"></div>
                        </td>
                      </tr>
                      <tr><td style="font-size: 8px; color: #10b981; font-weight: bold; font-family: sans-serif;">ESG</td></tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td class="email-content" style="padding: 20px 24px 10px 24px;">
        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f0f6ff; border-radius: 12px; border-collapse: separate; border: 1px solid #dbeafe;">
          <tr>
            <td style="padding: 16px 20px;">
              <table cellpadding="0" cellspacing="0" width="100%">
                <tr>
                  <td width="42" valign="middle">
                    <table cellpadding="0" cellspacing="0" style="background-color: #3b82f6; border-radius: 50%; width: 36px; height: 36px; text-align: center; border-collapse: separate;">
                      <tr><td style="font-size: 18px; color: #ffffff; text-align: center; vertical-align: middle;">✨</td></tr>
                    </table>
                  </td>
                  <td valign="middle" style="font-family: 'Outfit', 'Inter', sans-serif; padding-left: 12px;">
                    <div style="font-size: 14px; font-weight: 700; color: #1e3a8a;">Hello,</div>
                    <div style="font-size: 12px; color: #475569; margin-top: 2px; font-weight: 500; line-height: 1.4;">Here is your scheduled ESG update with the latest AI-generated summaries.</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td class="email-content" style="padding: 10px 24px 10px 24px;">
        {articles_list_html}
      </td>
    </tr>
    <tr>
      <td class="email-content" style="padding: 0 24px 24px 24px;">
        <table cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; border-collapse: separate; padding: 16px 8px;">
          <tr>
            <td class="footer-col" width="25%" align="center" style="font-family: 'Outfit', 'Inter', sans-serif; border-right: 1px solid #e2e8f0; vertical-align: top;">
              <div style="font-size: 18px; margin-bottom: 4px;">🌐</div>
              <div style="font-size: 10px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.05em;">Scrape</div>
              <div style="font-size: 9px; color: #64748b; margin-top: 2px; padding: 0 4px;">We monitor trusted sources</div>
            </td>
            <td class="footer-col" width="25%" align="center" style="font-family: 'Outfit', 'Inter', sans-serif; border-right: 1px solid #e2e8f0; padding: 0 4px; vertical-align: top;">
              <div style="font-size: 18px; margin-bottom: 4px;">🧠</div>
              <div style="font-size: 10px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.05em;">Generate</div>
              <div style="font-size: 9px; color: #64748b; margin-top: 2px; padding: 0 4px;">AI creates accurate summaries</div>
            </td>
            <td class="footer-col" width="25%" align="center" style="font-family: 'Outfit', 'Inter', sans-serif; border-right: 1px solid #e2e8f0; padding: 0 4px; vertical-align: top;">
              <div style="font-size: 18px; margin-bottom: 4px;">⏱️</div>
              <div style="font-size: 10px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.05em;">Schedule</div>
              <div style="font-size: 9px; color: #64748b; margin-top: 2px; padding: 0 4px;">Delivered to your schedule</div>
            </td>
            <td class="footer-col" width="25%" align="center" style="font-family: 'Outfit', 'Inter', sans-serif; padding: 0 4px; vertical-align: top;">
              <div style="font-size: 18px; margin-bottom: 4px;">✉️</div>
              <div style="font-size: 10px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.05em;">Send</div>
              <div style="font-size: 9px; color: #64748b; margin-top: 2px; padding: 0 4px;">Straight to your inbox</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding: 24px; background-color: #f8fafc; border-top: 1px solid #f1f5f9; text-align: center; border-bottom-left-radius: 16px; border-bottom-right-radius: 16px; font-family: 'Outfit', 'Inter', sans-serif;">
        <div style="font-size: 11px; color: #94a3b8; line-height: 1.5; margin-bottom: 12px;">
          You are receiving this email because you are subscribed to <strong>{plan_name_esc}</strong> updates.
        </div>
        <div style="font-size: 11px; color: #94a3b8; font-weight: 600;">
          <a href="#" style="color: #2563eb; text-decoration: none;">Unsubscribe</a> &nbsp;·&nbsp;
          <a href="#" style="color: #2563eb; text-decoration: none;">Manage Preferences</a>
        </div>
      </td>
    </tr>
  </table>
</body>
</html>
"""


# ── Sending (Brevo REST API only — no SMTP, no Resend) ─────────────────
# Railway blocks outbound SMTP ports, so all mail goes out over HTTPS via
# Brevo's transactional email API. Credentials are read exclusively from
# environment variables (BREVO_API_KEY / BREVO_FROM_EMAIL / BREVO_FROM_NAME) —
# never hardcoded, never required to be stored in the app DB.
MAX_SEND_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 2  # 2s, 4s, 8s
REQUEST_TIMEOUT_SECONDS = 30


class BrevoConfigError(RuntimeError):
    """Raised when Brevo credentials are missing/incomplete."""


class BrevoSendError(RuntimeError):
    """Raised when Brevo rejects a send after retries are exhausted."""


def get_brevo_config():
    """Reads Brevo credentials. Environment variables are the source of
    truth (see Railway → Variables); DB settings are only used as an
    optional convenience fallback for the sender display name/email so the
    Settings UI can still show something meaningful."""
    cfg = {}
    try:
        cfg = get_settings() or {}
    except Exception:
        cfg = {}
    return {
        "api_key": os.environ.get("BREVO_API_KEY"),
        "from_email": os.environ.get("BREVO_FROM_EMAIL") or cfg.get("brevo_from_email"),
        "from_name": os.environ.get("BREVO_FROM_NAME") or cfg.get("brevo_from_name") or "Insight Flow AI",
    }


def _as_list(value):
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _as_email_objects(value):
    return [{"email": e} for e in _as_list(value)]


def _build_attachments(attachments=None, inline_images=None):
    """Builds Brevo's flat `attachment` array. Regular attachments and
    inline images use the same shape — inline images are distinguished by
    their `name` matching a `cid:<name>` reference inside the HTML body."""
    out = []
    for a in _as_list(attachments):
        try:
            if a.get("content_base64"):
                b64 = a["content_base64"]
            elif a.get("path"):
                with open(a["path"], "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
            elif isinstance(a.get("content"), (bytes, bytearray)):
                b64 = base64.b64encode(a["content"]).decode("ascii")
            else:
                log.warning("Skipping attachment with no content/path/content_base64: %s", a.get("filename"))
                continue
            out.append({"content": b64, "name": a.get("filename") or a.get("name") or "attachment"})
        except OSError as e:
            log.warning("Could not read attachment %s: %s", a.get("path") or a.get("filename"), e)

    for img in _as_list(inline_images):
        try:
            if img.get("content_base64"):
                b64 = img["content_base64"]
            elif img.get("path"):
                with open(img["path"], "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
            else:
                continue
            name = img.get("cid") or img.get("name") or "image"
            out.append({"content": b64, "name": name})
        except OSError as e:
            log.warning("Could not read inline image %s: %s", img.get("path"), e)

    return out


def _post_to_brevo(payload, api_key):
    """POSTs to the Brevo API with retry/backoff for transient failures.
    Retries on: network errors, timeouts, HTTP 429, HTTP 5xx.
    Fails immediately (no retry) on: HTTP 4xx other than 429 — these are
    caused by bad payloads / invalid recipients / bad API keys and won't
    succeed on retry."""
    last_error = None

    for attempt in range(1, MAX_SEND_RETRIES + 1):
        try:
            resp = httpx.post(
                BREVO_API_URL,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as e:
            last_error = f"Brevo request timed out: {e}"
        except httpx.RequestError as e:
            last_error = f"Network error calling Brevo: {e}"
        else:
            if resp.status_code < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}

            try:
                err_body = resp.json()
            except ValueError:
                err_body = {"message": resp.text}

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else RETRY_BACKOFF_BASE_SECONDS ** attempt
                last_error = f"Brevo rate limit (429): {err_body}"
                if attempt < MAX_SEND_RETRIES:
                    log.warning("Brevo rate-limited, retrying in %.1fs (attempt %d/%d)", wait, attempt, MAX_SEND_RETRIES)
                    time.sleep(wait)
                    continue
                raise BrevoSendError(last_error)

            if 500 <= resp.status_code < 600:
                last_error = f"Brevo server error ({resp.status_code}): {err_body}"
                if attempt < MAX_SEND_RETRIES:
                    wait = RETRY_BACKOFF_BASE_SECONDS ** attempt
                    log.warning("Brevo 5xx, retrying in %ds (attempt %d/%d)", wait, attempt, MAX_SEND_RETRIES)
                    time.sleep(wait)
                    continue
                raise BrevoSendError(last_error)

            # 4xx (invalid recipient, bad API key, malformed payload, etc.) — don't retry.
            raise BrevoSendError(f"Brevo rejected the request ({resp.status_code}): {err_body}")

        # Network/timeout error path — retry with backoff.
        if attempt < MAX_SEND_RETRIES:
            wait = RETRY_BACKOFF_BASE_SECONDS ** attempt
            log.warning("%s — retrying in %ds (attempt %d/%d)", last_error, wait, attempt, MAX_SEND_RETRIES)
            time.sleep(wait)
            continue

    raise BrevoSendError(last_error or "Unknown Brevo send failure after retries")


def send_email(
    to,
    subject,
    html=None,
    text=None,
    cc=None,
    bcc=None,
    attachments=None,
    inline_images=None,
    reply_to=None,
    tags=None,
    plan_id=None,
    plan_name="",
    articles_count=0,
):
    """Low-level, reusable sender used by every other helper in this module.
    `to`/`cc`/`bcc` accept a single email string or a list of strings.
    Always writes a row to email_log (sent or failed) so /api/email-log and
    the Logs page have full visibility into every attempt.

    Returns: {"success": bool, "messageId": str|None, "status": "sent"|"failed", "error": str|None}
    """
    if not html and not text:
        raise ValueError("send_email requires either `html` or `text` content.")

    cfg = get_brevo_config()
    recipients = _as_list(to)
    recipient_label = ", ".join(recipients)
    ts = datetime.utcnow().isoformat() + "Z"
    log_id = f"el_{int(datetime.utcnow().timestamp() * 1000)}"

    def _log_and_return(success, message_id, error):
        save_email_log({
            "id": log_id,
            "planId": plan_id,
            "planName": plan_name or ("Manual Digest" if not plan_id else ""),
            "ts": ts,
            "recipient": recipient_label,
            "subject": subject,
            "articlesCount": articles_count,
            "status": "sent" if success else "failed",
            "error": error or "",
            "messageId": message_id,
        })
        return {
            "success": success,
            "messageId": message_id,
            "status": "sent" if success else "failed",
            "error": error,
        }

    if not cfg["api_key"] or not cfg["from_email"]:
        error = (
            "Brevo is not configured. Set BREVO_API_KEY and BREVO_FROM_EMAIL "
            "as environment variables in Railway → Variables, then redeploy."
        )
        log.error(error)
        return _log_and_return(False, None, error)

    payload = {
        "sender": {"name": cfg["from_name"], "email": cfg["from_email"]},
        "to": _as_email_objects(recipients),
        "subject": subject,
    }
    if html:
        payload["htmlContent"] = html
    if text:
        payload["textContent"] = text
    if cc:
        payload["cc"] = _as_email_objects(cc)
    if bcc:
        payload["bcc"] = _as_email_objects(bcc)
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    if tags:
        payload["tags"] = _as_list(tags)

    attach = _build_attachments(attachments, inline_images)
    if attach:
        payload["attachment"] = attach

    log.info("Sending email via Brevo | to=%s | subject=%s", recipient_label, subject)
    try:
        result = _post_to_brevo(payload, cfg["api_key"])
        message_id = result.get("messageId") if isinstance(result, dict) else None
        log.info("Brevo send succeeded | to=%s | messageId=%s", recipient_label, message_id)
        return _log_and_return(True, message_id, None)
    except Exception as e:
        error = str(e)
        log.error("Brevo send failed | to=%s | subject=%s | error=%s", recipient_label, subject, error)
        return _log_and_return(False, None, error)


def send_html_email(to, subject, html, **kwargs):
    """Convenience wrapper: send a pre-built HTML email."""
    return send_email(to, subject, html=html, **kwargs)


def send_report(to, subject, html, plan_id=None, plan_name="", articles_count=0, cc=None, bcc=None):
    """Sends an ESG digest / report email, auto-attaching the inline logo
    referenced as cid:logo.png inside compile_email_html()."""
    inline_images = None
    logo_path = _find_logo()
    if logo_path:
        inline_images = [{"cid": "logo.png", "path": logo_path}]
    return send_email(
        to, subject, html=html, cc=cc, bcc=bcc, inline_images=inline_images,
        plan_id=plan_id, plan_name=plan_name, articles_count=articles_count,
    )


def send_notification(to, subject, message, html=None, **kwargs):
    """Sends a simple plain-text style notification (alerts, status pings,
    scheduler failures, etc.). Pass `html` to override the auto-generated
    minimal HTML wrapper."""
    body_html = html or f"<div style='font-family:sans-serif;font-size:14px;color:#0f172a;'>{escape_html(message)}</div>"
    return send_email(to, subject, html=body_html, text=message, **kwargs)


def send_digest_for_plan(plan, articles):
    """Python port of sendEmailForPlan — used by the scheduler and by the
    "immediate" auto-mail path so digests go out with no browser open.
    Never raises: failures are logged and counted so the scheduler can
    continue on to the next plan/job."""
    active_groups = [g for g in (plan.get("recipientGroups") or []) if g.get("active")]
    recipients = sorted({e for g in active_groups for e in (g.get("emails") or [])})

    if not recipients or not articles:
        return {"sent": 0, "failed": 0}

    subject = f"{plan.get('name')} — {len(articles)} new article{'' if len(articles) == 1 else 's'}"
    html = compile_email_html(plan, articles)

    sent = failed = 0
    for to in recipients:
        try:
            result = send_report(
                to, subject, html,
                plan_id=plan.get("id"), plan_name=plan.get("name"), articles_count=len(articles),
            )
            if result["success"]:
                sent += 1
            else:
                failed += 1
        except Exception as e:
            # Defensive: send_report already catches send errors internally,
            # but we never want one bad recipient to kill the whole job.
            log.error("Unexpected error sending digest to %s: %s", to, e)
            failed += 1
    return {"sent": sent, "failed": failed}
