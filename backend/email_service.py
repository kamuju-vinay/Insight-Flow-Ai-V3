"""
Server-side email digest builder + sender.

This is a faithful Python port of `compileEmailHtml` / `sendEmailForPlan`
from src/App.jsx, so the email design is byte-for-byte the same regardless
of whether the send was triggered by the browser or by this backend's
scheduler. Do not change the HTML/CSS below without also updating App.jsx,
or the two will drift apart.

Email provider: Resend is used first (RESEND_API_KEY). If it's not
configured, or the send fails, we fall back to SMTP using the same
settings the app's Settings page already stores (smtp_host, smtp_port,
smtp_user, smtp_password, sender_email, sender_name).
"""
import os
import re
import base64
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

import httpx

from backend.db import get_settings, save_email_log

log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
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
    logo_src = "cid:logo"
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


# ── Sending ──────────────────────────────────────────────────────────────
def _send_via_resend(to, subject, html, sender_name, sender_email, api_key):
    payload = {
        "from": f"{sender_name} <{sender_email}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }
    logo_path = _find_logo()
    if logo_path:
        with open(logo_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        payload["attachments"] = [{"filename": "logo.png", "content": b64, "content_id": "logo"}]

    resp = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend error {resp.status_code}: {resp.text}")
    return resp.json()


def _send_via_brevo(to, subject, html, sender_name, sender_email, api_key):
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
    }
    logo_path = _find_logo()
    if logo_path:
        with open(logo_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        payload["attachment"] = [{"content": b64, "name": "logo.png"}]

    resp = httpx.post(
        BREVO_API_URL,
        headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo error {resp.status_code}: {resp.text}")
    return resp.json()


def _send_via_smtp(to, subject, html, cfg):
    smtp_host = cfg.get("smtp_host")
    smtp_port = int(cfg.get("smtp_port") or 587)
    smtp_user = cfg.get("smtp_user")
    smtp_password = cfg.get("smtp_password")
    sender_email = cfg.get("sender_email") or smtp_user
    sender_name = cfg.get("sender_name") or "Insight Flow AI"

    if not all([smtp_host, smtp_user, smtp_password]):
        raise RuntimeError("SMTP is not configured (set smtp_host/smtp_user/smtp_password in Settings).")

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = f'"{sender_name}" <{sender_email}>'
    msg["To"] = to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    logo_path = _find_logo()
    if logo_path:
        with open(logo_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<logo>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)

    is_ssl = smtp_port == 465
    server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) if is_ssl else smtplib.SMTP(smtp_host, smtp_port, timeout=30)
    try:
        if not is_ssl:
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(sender_email, [to], msg.as_string())
    finally:
        server.quit()


def send_email(to, subject, html, articles_count=0, plan_id=None, plan_name=""):
    """Sends one email. Tries Brevo first, then Resend, then SMTP. Always logs to email_log."""
    cfg = get_settings()
    brevo_key = cfg.get("brevo_api_key") or os.environ.get("BREVO_API_KEY")
    resend_key = cfg.get("resend_api_key") or os.environ.get("RESEND_API_KEY")
    sender_email = (
        cfg.get("brevo_from_email")
        or os.environ.get("BREVO_FROM_EMAIL")
        or cfg.get("resend_from_email")
        or os.environ.get("RESEND_FROM_EMAIL")
        or cfg.get("sender_email")
        or "onboarding@resend.dev"
    )
    sender_name = (
        cfg.get("brevo_from_name")
        or cfg.get("resend_from_name")
        or cfg.get("sender_name")
        or "Insight Flow AI"
    )

    log_id = f"el_{int(datetime.utcnow().timestamp() * 1000)}"
    status, error = "pending", ""
    errors = []

    providers = []
    if brevo_key:
        providers.append(("brevo", lambda: _send_via_brevo(to, subject, html, sender_name, sender_email, brevo_key)))
    if resend_key:
        providers.append(("resend", lambda: _send_via_resend(to, subject, html, sender_name, sender_email, resend_key)))
    providers.append(("smtp", lambda: _send_via_smtp(to, subject, html, cfg)))

    for name, send_fn in providers:
        try:
            send_fn()
            status = "sent"
            break
        except Exception as e:
            errors.append(f"{name} failed ({e})")
            status = "failed"

    error = "; ".join(errors) if status != "sent" else ""

    save_email_log({
        "id": log_id,
        "planId": plan_id,
        "planName": plan_name or ("Manual Digest" if not plan_id else ""),
        "ts": datetime.utcnow().isoformat() + "Z",
        "recipient": to,
        "subject": subject,
        "articlesCount": articles_count,
        "status": status,
        "error": error,
    })
    if status != "sent":
        log.warning("Email to %s failed: %s", to, error)
    return status, error


def send_digest_for_plan(plan, articles):
    """Python port of sendEmailForPlan — used by the scheduler and by the
    "immediate" auto-mail path so digests go out with no browser open."""
    active_groups = [g for g in (plan.get("recipientGroups") or []) if g.get("active")]
    recipients = sorted({e for g in active_groups for e in (g.get("emails") or [])})

    if not recipients or not articles:
        return {"sent": 0, "failed": 0}

    subject = f"{plan.get('name')} — {len(articles)} new article{'' if len(articles) == 1 else 's'}"
    html = compile_email_html(plan, articles)

    sent = failed = 0
    for to in recipients:
        status, _ = send_email(
            to, subject, html,
            articles_count=len(articles), plan_id=plan.get("id"), plan_name=plan.get("name"),
        )
        if status == "sent":
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}
