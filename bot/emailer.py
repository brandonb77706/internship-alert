"""Build and send the alert email (mobile-friendly HTML via Gmail SMTP)."""

from __future__ import annotations

import html
import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .models import Job

log = logging.getLogger("bot.emailer")


# ---------------------------------------------------------------------------
# Priority tagging
# ---------------------------------------------------------------------------
def mark_priorities(jobs: list[Job], cfg: dict) -> None:
    # Whole-word match so "Intuit" doesn't match "Intuitive Surgical" and
    # "Apple" doesn't match "Applebee's".
    patterns = [
        re.compile(r"\b" + re.escape(p.lower().strip()) + r"\b")
        for p in cfg.get("priority_companies", [])
        if p.strip()
    ]
    for job in jobs:
        c = job.company.lower()
        job.is_priority = any(pat.search(c) for pat in patterns)


# ---------------------------------------------------------------------------
# Subject line
# ---------------------------------------------------------------------------
def build_subject(jobs: list[Job]) -> str:
    n = len(jobs)
    # unique companies, priority first, preserving order
    seen: list[str] = []
    for job in sorted(jobs, key=lambda j: (not j.is_priority,)):
        if job.company not in seen:
            seen.append(job.company)
    plural = "internship" if n == 1 else "internships"
    if not seen:
        return f"🚨 {n} new SWE {plural}"
    if len(seen) <= 2:
        names = ", ".join(seen)
    else:
        names = f"{seen[0]}, {seen[1]} +{len(seen) - 2}"
    return f"🚨 {n} new SWE {plural} — {names}"


# ---------------------------------------------------------------------------
# HTML body
# ---------------------------------------------------------------------------
def build_html(jobs: list[Job], cfg: dict) -> str:
    max_jobs = int(cfg.get("email", {}).get("max_jobs_per_email", 60))
    shown = jobs[:max_jobs]
    overflow = len(jobs) - len(shown)

    priority = [j for j in shown if j.is_priority]
    others = [j for j in shown if not j.is_priority]

    sections = []
    if priority:
        sections.append(
            _section("⭐ Priority Targets", priority, highlight=True)
        )
    if others:
        sections.append(_section("Everything Else", others, highlight=False))

    overflow_note = ""
    if overflow > 0:
        overflow_note = (
            f'<p style="color:#64748b;font-size:14px;text-align:center;'
            f'margin:16px 0;">…and {overflow} more new posting(s) recorded '
            f"(trimmed to keep this email readable).</p>"
        )

    body = "".join(sections) + overflow_note
    total = len(jobs)
    plural = "internship" if total == 1 else "internships"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>New SWE internships</title>
<style>
  @media only screen and (max-width:600px) {{
    .container {{ width:100% !important; padding:12px !important; }}
    .job-title {{ font-size:17px !important; }}
    .apply-btn {{ display:block !important; text-align:center !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
             -webkit-text-size-adjust:100%;">
  <div class="container" style="max-width:600px;margin:0 auto;padding:20px;">
    <div style="text-align:center;padding:8px 0 20px;">
      <div style="font-size:22px;font-weight:800;color:#0f172a;">
        {total} new SWE {plural} 🚀
      </div>
      <div style="font-size:13px;color:#64748b;margin-top:4px;">
        Summer 2027 · US / Remote
      </div>
    </div>
    {body}
    <div style="text-align:center;color:#94a3b8;font-size:12px;
                margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f0;">
      Sent by your internship-alert-bot · edit targets in config.yaml
    </div>
  </div>
</body>
</html>"""


def _section(heading: str, jobs: list[Job], highlight: bool) -> str:
    bg = "#fffbeb" if highlight else "#ffffff"
    border = "#f59e0b" if highlight else "#e2e8f0"
    head_color = "#b45309" if highlight else "#0f172a"
    cards = "".join(_card(j, highlight) for j in jobs)
    return f"""
    <div style="margin-bottom:22px;">
      <div style="font-size:16px;font-weight:700;color:{head_color};
                  margin:0 0 10px 4px;">{html.escape(heading)}
        <span style="color:#94a3b8;font-weight:500;">({len(jobs)})</span>
      </div>
      <div style="background:{bg};border:1px solid {border};border-radius:12px;
                  overflow:hidden;">
        {cards}
      </div>
    </div>"""


def _card(job: Job, highlight: bool) -> str:
    company = html.escape(job.company)
    title = html.escape(job.title)
    location = html.escape(job.location) if job.location else "Location N/A"
    posted = html.escape(job.date_posted) if job.date_posted else ""
    source = html.escape(job.source)
    url = html.escape(job.url, quote=True)

    meta_bits = [f"📍 {location}"]
    if posted:
        meta_bits.append(f"🗓 {posted}")
    meta_bits.append(f"🔎 {source}")
    meta = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta_bits)

    btn_bg = "#b45309" if highlight else "#2563eb"

    return f"""
    <div style="padding:14px 16px;border-bottom:1px solid #eef2f7;">
      <div style="font-size:13px;font-weight:800;color:#0f172a;
                  text-transform:uppercase;letter-spacing:.3px;">{company}</div>
      <div class="job-title" style="font-size:16px;font-weight:600;color:#1e293b;
                  margin:2px 0 6px;">{title}</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:12px;">{meta}</div>
      <a class="apply-btn" href="{url}"
         style="display:inline-block;background:{btn_bg};color:#ffffff;
                text-decoration:none;font-size:15px;font-weight:700;
                padding:11px 22px;border-radius:8px;">Apply&nbsp;→</a>
    </div>"""


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str, creds: dict, cfg: dict) -> None:
    sender = creds["gmail_address"]
    password = creds["gmail_app_password"]
    recipient = creds["recipient"]
    if not (sender and password and recipient):
        raise RuntimeError(
            "Missing email credentials. Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, "
            "and RECIPIENT_EMAIL (or email.recipient_override in config.yaml)."
        )

    email_cfg = cfg.get("email", {})
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 465))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(_plaintext_fallback(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("Sending email to %s via %s:%d", recipient, host, port)
    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    log.info("Email sent.")


def _plaintext_fallback(_html_body: str) -> str:
    return (
        "New SWE internships were posted. View this email in an HTML-capable "
        "client to see the job cards and apply links."
    )
