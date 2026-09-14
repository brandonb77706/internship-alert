"""Direct company career-feed readers.

Supports Greenhouse, Lever, Workday, Ashby, and SmartRecruiters.

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

# Phrases used to query Workday boards. Overridable globally via
# `workday_search_terms` in config.yaml, or per-company via `search_terms`.
_DEFAULT_WORKDAY_TERMS = (
    "software engineer intern",
    "software developer intern",
    "technology intern",
    "information technology intern",
)


def fetch_company(http: Http, company: dict, cfg: dict | None = None) -> list[Job]:
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
            return _workday(http, company, (cfg or {}).get("workday_search_terms"))
        if ats == "ashby":
            return _ashby(http, company)
        if ats == "smartrecruiters":
            return _smartrecruiters(http, company)
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
def _workday(http: Http, company: dict, cfg_terms=None) -> list[Job]:
    name = company["name"]
    host = company.get("host", "").strip()
    tenant = company.get("tenant", "").strip()
    site = company.get("site", "").strip()
    if not (host and tenant and site):
        log.info("%s: incomplete workday config — skipping", name)
        return []

    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    # Workday's search is fuzzy and ranks badly, so no single phrase has good
    # recall: "software engineer intern" misses PNC's "Technology Undergraduate
    # Intern", while plain "intern" returns 1400+ rows (it matches "internal"
    # and "international"). Running a few targeted phrases and merging gets the
    # coverage without crawling the whole board.
    terms = company.get("search_terms") or cfg_terms or _DEFAULT_WORKDAY_TERMS
    page_size = 20  # Workday's per-request maximum
    max_pages = int(company.get("max_pages", 3))

    jobs: list[Job] = []
    seen_paths: set[str] = set()
    for term in terms:
        offset = 0
        for _ in range(max_pages):
            body = {
                "appliedFacets": {},
                "limit": page_size,
                "offset": offset,
                "searchText": term,
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
                if not ext or ext in seen_paths:
                    continue
                seen_paths.add(ext)
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
    log.info(
        "%s (Workday): %d intern rows across %d search term(s)",
        name, len(jobs), len(terms),
    )
    return jobs


# ---- Ashby -----------------------------------------------------------------
def _ashby(http: Http, company: dict) -> list[Job]:
    name = company["name"]
    token = company.get("token", "").strip()
    if not token:
        log.info("%s: no ashby token — skipping", name)
        return []
    # includeCompensation=false keeps the payload smaller; descriptions still
    # come along, which is why we filter on title before building any Job.
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        "?includeCompensation=false"
    )
    resp = http.get(url)
    if resp is None:
        return []
    jobs: list[Job] = []
    for j in resp.json().get("jobs", []):
        title = j.get("title", "")
        if not _is_intern(title):
            continue
        apply_url = j.get("jobUrl") or j.get("applyUrl", "")
        if not apply_url:
            continue
        loc = j.get("location") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        posted = _parse_iso(j.get("publishedAt"))
        jobs.append(
            Job(
                company=name,
                title=title,
                url=apply_url,
                location=loc,
                date_posted=_fmt(posted),
                source=f"Ashby:{name}",
                posted_date=posted,
            )
        )
    log.info("%s (Ashby): %d intern rows", name, len(jobs))
    return jobs


# ---- SmartRecruiters --------------------------------------------------------
def _smartrecruiters(http: Http, company: dict) -> list[Job]:
    """Enterprise boards here are huge (Bosch is ~5k jobs), so we lean on the
    API's own `q` and `country` filters instead of pulling the whole board."""
    name = company["name"]
    token = company.get("token", "").strip()
    if not token:
        log.info("%s: no smartrecruiters token — skipping", name)
        return []

    endpoint = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    jobs: list[Job] = []
    page_size = 100  # API maximum
    offset = 0
    max_pages = 5
    for _ in range(max_pages):
        params = {
            "limit": page_size,
            "offset": offset,
            "q": "intern",
            "country": company.get("country", "us"),
        }
        resp = http.get(endpoint, params=params)
        if resp is None:
            break
        data = resp.json()
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            title = p.get("name", "")
            if not _is_intern(title):
                continue
            posting_id = p.get("id")
            if not posting_id:
                continue
            identifier = (p.get("company") or {}).get("identifier", token)
            apply_url = f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
            loc = (p.get("location") or {}).get("fullLocation", "")
            posted = _parse_iso(p.get("releasedDate"))
            jobs.append(
                Job(
                    company=name,
                    title=title,
                    url=apply_url,
                    location=loc,
                    date_posted=_fmt(posted),
                    source=f"SmartRecruiters:{name}",
                    posted_date=posted,
                )
            )
        offset += page_size
        if offset >= data.get("totalFound", 0):
            break
    log.info("%s (SmartRecruiters): %d intern rows", name, len(jobs))
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
