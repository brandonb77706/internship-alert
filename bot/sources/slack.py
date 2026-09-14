"""Read job links out of Slack channels via the Slack Web API.

Unlike the ATS feeds, Slack messages are free-form prose, so this source is
best-effort by design: we pull every link out of recent messages, work out the
company from the link itself where we can, and hand the result to filters.py to
accept or reject. Anything we can't make sense of is dropped rather than guessed
at, so the worst case is a missed posting, not a junk alert.

Requires a Slack bot token in SLACK_BOT_TOKEN with `channels:history` (public
channels) and/or `groups:history` (private channels), plus the bot being a
member of each channel it reads. See README "Reading a Slack channel".
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..http import Http
from ..models import Job

log = logging.getLogger("bot.sources.slack")

_HISTORY_URL = "https://slack.com/api/conversations.history"

# <https://example.com|display text> and <https://example.com>
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
# Bare URLs, for messages pasted without Slack auto-linking them.
_BARE_URL = re.compile(r"(?<![<|])(https?://[^\s<>|]+)")

# Hosts we can reliably pull a company slug out of. Ordered: first match wins.
_COMPANY_FROM_PATH = (
    ("boards.greenhouse.io", 0),
    ("job-boards.greenhouse.io", 0),
    ("jobs.lever.co", 0),
    ("jobs.ashbyhq.com", 0),
    ("jobs.smartrecruiters.com", 0),
)

_INTERN_HINTS = ("intern", "internship", "co-op", "coop", "new grad", "newgrad")

# Link hosts that are never a job posting — Slack's own files, image hosts, and
# the usual chat noise. Keeps obvious junk out before filters.py runs.
_SKIP_HOSTS = (
    "slack.com",
    "files.slack.com",
    "giphy.com",
    "tenor.com",
    "imgur.com",
    "youtube.com",
    "youtu.be",
    "docs.google.com",
    "twitter.com",
    "x.com",
)


def fetch_all(http: Http, cfg: dict) -> list[Job]:
    """Read every configured Slack channel. Returns [] if Slack isn't set up."""
    slack_cfg = cfg.get("slack", {})
    if not slack_cfg.get("enabled", False):
        return []

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        log.warning(
            "slack.enabled is true but SLACK_BOT_TOKEN is unset — skipping Slack."
        )
        return []

    channels = [c for c in slack_cfg.get("channels", []) if c.get("id")]
    if not channels:
        log.info("Slack enabled but no channels configured — skipping.")
        return []

    lookback_hours = int(slack_cfg.get("lookback_hours", 48))
    oldest = (
        datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    ).timestamp()

    jobs: list[Job] = []
    for channel in channels:
        try:
            jobs.extend(_fetch_channel(http, token, channel, oldest, slack_cfg))
        except Exception as exc:  # one bad channel never kills the run
            log.warning(
                "Slack channel %s failed: %s", channel.get("name", channel["id"]), exc
            )
    log.info("Slack: %d job links across %d channel(s)", len(jobs), len(channels))
    return jobs


def _fetch_channel(
    http: Http, token: str, channel: dict, oldest: float, slack_cfg: dict
) -> list[Job]:
    label = channel.get("name") or channel["id"]
    headers = {"Authorization": f"Bearer {token}"}
    jobs: list[Job] = []
    cursor = ""
    max_pages = int(slack_cfg.get("max_pages_per_channel", 5))

    for _ in range(max_pages):
        params = {
            "channel": channel["id"],
            "oldest": f"{oldest:.6f}",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        resp = http.get(_HISTORY_URL, params=params, headers=headers)
        if resp is None:
            break
        data = resp.json()
        # Slack returns HTTP 200 with ok:false for auth/scope/membership errors,
        # so the body is the only reliable signal.
        if not data.get("ok"):
            log.warning(
                "Slack %s: API error %r — check the bot's scopes and that it has "
                "been invited to the channel.",
                label,
                data.get("error", "unknown"),
            )
            break
        for msg in data.get("messages", []):
            jobs.extend(_jobs_from_message(msg, label, channel))
        cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break

    log.info("Slack #%s: %d job links", label, len(jobs))
    return jobs


def _jobs_from_message(msg: dict, label: str, channel: dict) -> list[Job]:
    if msg.get("subtype") in ("channel_join", "channel_leave"):
        return []

    text = msg.get("text", "") or ""
    # Bot posts (e.g. an RSS or jobs integration) put the useful text in
    # attachments/blocks rather than the top-level `text` field.
    text = "\n".join([text, *_attachment_text(msg)]).strip()
    if not text:
        return []

    posted = _ts_to_date(msg.get("ts"))
    default_company = channel.get("company_hint", "")

    jobs: list[Job] = []
    for url, anchor in _links(text):
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if not host or any(host.endswith(h) for h in _SKIP_HOSTS):
            continue

        title = _clean(anchor) or _title_from_text(text)
        if not title:
            continue
        # Require an internship signal somewhere in the message, otherwise a
        # channel full of chatter would hand filters.py hundreds of stray links.
        haystack = f"{title} {text}".lower()
        if not any(h in haystack for h in _INTERN_HINTS):
            continue

        company = _company_from_url(url) or default_company or host
        jobs.append(
            Job(
                company=company,
                title=title,
                url=url,
                location="",  # Slack messages rarely state it in a parseable way
                date_posted=_fmt(posted),
                source=f"Slack:#{label}",
                posted_date=posted,
            )
        )
    return jobs


def _attachment_text(msg: dict) -> list[str]:
    out: list[str] = []
    for att in msg.get("attachments", []) or []:
        for key in ("title", "text", "fallback"):
            if att.get(key):
                out.append(str(att[key]))
        if att.get("title_link"):
            out.append(str(att["title_link"]))
    for block in msg.get("blocks", []) or []:
        txt = (block.get("text") or {}).get("text")
        if txt:
            out.append(str(txt))
        for field in block.get("fields", []) or []:
            if field.get("text"):
                out.append(str(field["text"]))
    return out


def _links(text: str) -> list[tuple[str, str]]:
    """Return (url, anchor_text) pairs, Slack-style links first."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _SLACK_LINK.finditer(text):
        url = m.group(1).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            found.append((url, m.group(2) or ""))
    for m in _BARE_URL.finditer(text):
        url = m.group(1).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            found.append((url, ""))
    return found


def _company_from_url(url: str) -> str:
    """Pull the company slug out of a known ATS URL, e.g.
    boards.greenhouse.io/stripe/jobs/123 -> "Stripe"."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = [s for s in parts.path.split("/") if s]

    for known_host, idx in _COMPANY_FROM_PATH:
        if host.endswith(known_host) and len(segments) > idx:
            return _prettify(segments[idx])

    if host.endswith("myworkdayjobs.com"):
        # <tenant>.wdN.myworkdayjobs.com
        return _prettify(host.split(".")[0])
    return ""


def _prettify(slug: str) -> str:
    words = re.split(r"[-_]+", slug)
    return " ".join(w.capitalize() if w.islower() else w for w in words if w)


def _title_from_text(text: str) -> str:
    """Fall back to the first non-empty line that isn't just a URL."""
    for line in text.splitlines():
        cleaned = _clean(line)
        if cleaned and not cleaned.lower().startswith("http"):
            return cleaned
    return ""


def _clean(value: str) -> str:
    """Strip Slack markup (links, mentions, emphasis) down to plain text."""
    s = _SLACK_LINK.sub(lambda m: m.group(2) or "", value or "")
    s = re.sub(r"<[@#!][^>]+>", "", s)          # @user, #channel, !here
    s = re.sub(r"[*_~`]+", "", s)               # bold/italic/strike/code
    s = re.sub(r":[a-z0-9_+-]+:", "", s)        # :emoji:
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -–—|·•").strip()


def _ts_to_date(ts) -> date | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except (TypeError, ValueError):
        return None


def _fmt(d) -> str:
    return d.strftime("%b %d, %Y") if d else ""
