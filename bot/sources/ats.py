"""Direct company career-feed readers for Greenhouse, Lever, and Workday.

Every feed is wrapped in try/except at the call site (main.py) *and* returns []
on any error here, so one broken company can never kill the run.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from ..http import Http
from ..models import Job

log = logging.getLogger("bot.sources.ats")

# Keywords used to pre-filter feeds server-side / client-side before the main
# filter runs. Broad on purpose — the real filtering happens in filters.py.
_INTERN_HINTS = ("intern", "internship")


def fetch_company(http: Http, company: dict) -> list[Job]:
    """Dispatch to the right ATS reader based on `type`."""
    if not company.get("enabled", False):
        return []
    name = company.get("name", "?")
    ats = (company.get("type") or "").lower()
    try:
        if ats == "greenhouse":
            return _greenhouse(http, company)
        if ats == "lever":
            return _lever(http, company)
        if ats == "workday":
            return _workday(http, company)
        log.warning("%s: unknown ATS type %r — skipping", name, ats)
        return []
    except Exception as exc:  # last-resort guard; never propagate
        log.warning("%s: feed error (%s) — skipping", name, exc)
        return []


def _is_intern(title: str) -> bool:
    t = title.lower()
    return any(h in t for h in _INTERN_HINTS)


# ---- Greenhouse ------------------------------------------------------------
def _greenhouse(http: Http, company: dict) -> list[Job]:
    name = company["name"]
    token = company.get("token", "").strip()
    if not token:
        log.info("%s: no greenhouse token — skipping", name)
        return []
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    resp = http.get(url)
    if resp is None:
        return []
    data = resp.json()
    jobs: list[Job] = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _is_intern(title):
            continue
        loc = (j.get("location") or {}).get("name", "")
        apply_url = j.get("absolute_url", "")
        if not apply_url:
            continue
        posted = _parse_iso(j.get("updated_at") or j.get("first_published"))
        jobs.append(
            Job(
                company=name,
                title=title,
                url=apply_url,
                location=loc,
                date_posted=_fmt(posted),
                source=f"Greenhouse:{name}",
                posted_date=posted,
            )
        )
    log.info("%s (Greenhouse): %d intern rows", name, len(jobs))
    return jobs


# ---- Lever -----------------------------------------------------------------
def _lever(http: Http, company: dict) -> list[Job]:
    name = company["name"]
    token = company.get("token", "").strip()
    if not token:
        log.info("%s: no lever token — skipping", name)
        return []
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    resp = http.get(url)
    if resp is None:
        return []
    jobs: list[Job] = []
    for j in resp.json():
        title = j.get("text", "")
        if not _is_intern(title):
            continue
        cats = j.get("categories") or {}
        loc = cats.get("location", "")
        apply_url = j.get("hostedUrl") or j.get("applyUrl", "")
        if not apply_url:
            continue
        posted = None
        if j.get("createdAt"):
            posted = datetime.fromtimestamp(
                j["createdAt"] / 1000, tz=timezone.utc
            ).date()
        jobs.append(
            Job(
                company=name,
                title=title,
                url=apply_url,
                location=loc,
                date_posted=_fmt(posted),
                source=f"Lever:{name}",
                posted_date=posted,
            )
        )
    log.info("%s (Lever): %d intern rows", name, len(jobs))
    return jobs


# ---- Workday ---------------------------------------------------------------
def _workday(http: Http, company: dict) -> list[Job]:
    name = company["name"]
    host = company.get("host", "").strip()
    tenant = company.get("tenant", "").strip()
    site = company.get("site", "").strip()
    if not (host and tenant and site):
        log.info("%s: incomplete workday config — skipping", name)
        return []

    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs: list[Job] = []
    offset = 0
    page_size = 20
    max_pages = 5  # search is pre-filtered to "intern"; don't crawl the world
    for _ in range(max_pages):
        body = {
            "appliedFacets": {},
            "limit": page_size,
            "offset": offset,
            "searchText": "software engineer intern",
        }
        resp = http.post(endpoint, json=body)
        if resp is None:
            break
        data = resp.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            title = p.get("title", "")
            if not _is_intern(title):
                continue
            ext = p.get("externalPath", "")
            if not ext:
                continue
            apply_url = f"https://{host}/en-US/{site}{ext}"
            loc = p.get("locationsText", "")
            posted = _parse_workday_posted(p.get("postedOn", ""))
            jobs.append(
                Job(
                    company=name,
                    title=title,
                    url=apply_url,
                    location=loc,
                    date_posted=p.get("postedOn", "") or _fmt(posted),
                    source=f"Workday:{name}",
                    posted_date=posted,
                )
            )
        offset += page_size
        if offset >= data.get("total", 0):
            break
    log.info("%s (Workday): %d intern rows", name, len(jobs))
    return jobs


# ---- helpers ---------------------------------------------------------------
def _parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _parse_workday_posted(value: str):
    """Workday uses 'Posted Today', 'Posted 3 Days Ago', 'Posted 30+ Days Ago'."""
    if not value:
        return None
    s = value.lower()
    from datetime import timedelta

    if "today" in s:
        return date.today()
    if "yesterday" in s:
        return date.today() - timedelta(days=1)
    import re

    m = re.search(r"(\d+)\s*day", s)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    return None


def _fmt(d) -> str:
    return d.strftime("%b %d, %Y") if d else ""
