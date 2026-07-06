"""Read internship-tracker GitHub repos and parse their markdown job tables.

We fetch the raw README via raw.githubusercontent.com (no API token needed for
public repos, and no rate-limit headaches). Each repo publishes a markdown
table of jobs; the columns vary between repos, so the parser maps columns by
their header names rather than by position.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..http import Http
from ..models import Job

log = logging.getLogger("bot.sources.github")

RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

# ---- column header -> canonical field --------------------------------------
_COMPANY_HEADERS = {"company", "name", "employer"}
_ROLE_HEADERS = {"role", "position", "title", "job title", "role title"}
_LOCATION_HEADERS = {"location", "locations", "loc"}
_LINK_HEADERS = {"application/link", "apply", "link", "application", "links", "application / link"}
_DATE_HEADERS = {"date posted", "date", "posted", "age", "date added"}

_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
# Verified badges / decorative glyphs some repos append to company names.
_BADGES = re.compile(r"[✓✅⭐🌟🔥🆕🎯💰🏆🔒→«↳©®™⭐️]")


def fetch(http: Http, repo_cfg: dict) -> list[Job]:
    """Fetch + parse all configured files for one repo (with fallback)."""
    name = repo_cfg.get("name", repo_cfg.get("repo", "?"))
    repos_to_try = [repo_cfg.get("repo", "")]
    if repo_cfg.get("fallback_repo"):
        repos_to_try.append(repo_cfg["fallback_repo"])
    branch = repo_cfg.get("branch", "main")
    files = repo_cfg.get("files", ["README.md"])

    for repo in repos_to_try:
        if not repo:
            continue
        jobs: list[Job] = []
        got_any = False
        for path in files:
            text = _fetch_raw(http, repo, branch, path)
            if text is None and branch != "main":
                text = _fetch_raw(http, repo, "main", path)  # branch fallback
            if text is None:
                continue
            got_any = True
            parsed = _parse(text, source=name)
            log.info("  %s/%s@%s: parsed %d rows", repo, path, branch, len(parsed))
            jobs.extend(parsed)
        if got_any:
            log.info("Source %s (%s): %d jobs", name, repo, len(jobs))
            return jobs
        log.warning("Repo %s unreachable, trying fallback", repo)

    log.warning("Source %s: no reachable repo", name)
    return []


def _fetch_raw(http: Http, repo: str, branch: str, path: str) -> str | None:
    url = RAW_URL.format(repo=repo, branch=branch, path=path)
    resp = http.get(url)
    return resp.text if resp is not None else None


# ---------------------------------------------------------------------------
def _parse(text: str, source: str) -> list[Job]:
    """Repos use either markdown tables (speedyapply/vanshb03/zshah101) or HTML
    tables (SimplifyJobs). Run both parsers; whichever matches wins, and the
    within-run dedupe collapses any overlap."""
    jobs = _parse_markdown_tables(text, source)
    if "<table" in text.lower():
        jobs.extend(_parse_html_tables(text, source))
    return jobs


def _parse_html_tables(text: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    soup = BeautifulSoup(text, "html.parser")
    for table in soup.find_all("table"):
        headers = [
            _TAG.sub("", (th.get_text() or "")).strip().lower()
            for th in table.find_all("th")
        ]
        colmap = _map_columns(headers)
        if "role" not in colmap:
            continue
        last_company = ""
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            job, last_company = _html_row_to_job(tds, colmap, source, last_company)
            if job is not None:
                jobs.append(job)
    return jobs


def _html_row_to_job(tds, colmap, source, last_company):
    def td(key):
        idx = colmap.get(key)
        if idx is None or idx >= len(tds):
            return None
        return tds[idx]

    def text(cell):
        # separator avoids gluing sibling nodes together (e.g. "CAIrvine")
        return cell.get_text(separator=" ", strip=True) if cell is not None else ""

    company_cell = td("company")
    company = ""
    if company_cell is not None:
        a = company_cell.find("a")
        company = _clean_text(text(a) if a else text(company_cell))
    if company in {"", "↳", "→", "«"}:
        company = last_company
    else:
        last_company = company

    role = _clean_text(text(td("role")))
    location = _clean_text(text(td("location")))
    date_raw = _clean_text(text(td("date")))

    link_cell = td("link")
    url = _pick_apply_href(link_cell)
    if not url and company_cell is not None:
        a = company_cell.find("a")
        url = _norm_url(a["href"]) if a and a.get("href") else ""

    if not role or not url:
        return None, last_company

    # Skip explicitly closed roles (SimplifyJobs shows a 🔒 lock, no apply link).
    cell_text = link_cell.get_text() if link_cell else ""
    if "🔒" in cell_text or "closed" in cell_text.lower():
        return None, last_company

    posted = _parse_date(date_raw)
    job = Job(
        company=company or "Unknown",
        title=role,
        url=url,
        location=location,
        date_posted=date_raw,
        source=source,
        posted_date=posted,
    )
    return job, last_company


def _pick_apply_href(cell) -> str:
    """Return the raw apply URL from an Application cell, skipping the
    simplify.jobs tracking redirect so the link goes straight to the ATS."""
    if cell is None:
        return ""
    hrefs = [a["href"] for a in cell.find_all("a") if a.get("href")]
    for h in hrefs:
        if "simplify.jobs" not in h.lower():
            return _norm_url(h)
    return _norm_url(hrefs[0]) if hrefs else ""


# ---------------------------------------------------------------------------
def _parse_markdown_tables(text: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    lines = text.splitlines()
    i = 0
    last_company = ""
    while i < len(lines):
        line = lines[i]
        # A table header row looks like: | Company | Role | ... |
        if _looks_like_table_row(line) and i + 1 < len(lines) and _is_divider(lines[i + 1]):
            headers = _split_row(line)
            colmap = _map_columns(headers)
            i += 2  # skip header + divider
            while i < len(lines) and _looks_like_table_row(lines[i]):
                cells = _split_row(lines[i])
                job, last_company = _row_to_job(cells, colmap, source, last_company)
                if job is not None:
                    jobs.append(job)
                i += 1
            continue
        i += 1
    return jobs


def _looks_like_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_divider(line: str) -> bool:
    s = line.strip()
    return bool(re.match(r"^\|[\s:|-]+\|?\s*$", s)) and "-" in s


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _map_columns(headers: list[str]) -> dict:
    colmap: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        h = _TAG.sub("", raw).strip().lower()
        if h in _COMPANY_HEADERS and "company" not in colmap:
            colmap["company"] = idx
        elif h in _ROLE_HEADERS and "role" not in colmap:
            colmap["role"] = idx
        elif h in _LOCATION_HEADERS and "location" not in colmap:
            colmap["location"] = idx
        elif h in _LINK_HEADERS and "link" not in colmap:
            colmap["link"] = idx
        elif h in _DATE_HEADERS and "date" not in colmap:
            colmap["date"] = idx
    return colmap


def _row_to_job(cells, colmap, source, last_company):
    if "role" not in colmap:
        return None, last_company

    def cell(key):
        idx = colmap.get(key)
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx]

    company_raw = cell("company")
    company = _clean_company(company_raw)
    # '↳' / '<--' / blank means "same company as the row above".
    if company in {"", "↳", "->", "→", "«", "same"} or company_raw.strip() in {"↳", "«", "→"}:
        company = last_company
    else:
        last_company = company

    role = _clean_text(cell("role"))
    location = _clean_text(cell("location"))
    date_raw = _clean_text(cell("date"))

    url = _extract_url(cell("link")) or _extract_url(company_raw) or _extract_url(cell("role"))
    if not role or not url:
        return None, last_company

    # Skip closed roles (SimplifyJobs marks these with a 🔒 lock).
    if "🔒" in cell("link") or "closed" in cell("link").lower():
        return None, last_company

    posted = _parse_date(date_raw)
    job = Job(
        company=company or "Unknown",
        title=role,
        url=url,
        location=location,
        date_posted=date_raw,
        source=source,
        posted_date=posted,
    )
    return job, last_company


def _clean_company(raw: str) -> str:
    m = _MD_LINK.search(raw)
    if m and m.group(1).strip():
        return _clean_text(m.group(1))
    b = _BOLD.search(raw)
    if b:
        return _clean_text(b.group(1))
    return _clean_text(raw)


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    t = _MD_LINK.sub(r"\1", raw)  # [text](url) -> text
    t = _TAG.sub("", t)           # strip html tags
    t = t.replace("**", "").replace("`", "")
    t = t.replace("↳", "")
    t = _BADGES.sub("", t)  # drop verified-badge / decorative glyphs
    return " ".join(t.split())


def _extract_url(raw: str) -> str:
    if not raw:
        return ""
    m = _HREF.search(raw)
    if m:
        return _norm_url(m.group(1))
    m = _MD_LINK.search(raw)
    if m:
        return _norm_url(m.group(2))
    m = re.search(r"https?://\S+", raw)
    if m:
        return _norm_url(m.group(0).rstrip(")>\"'"))
    return ""


def _norm_url(u: str) -> str:
    """Clean the apply URL: strip wrapping punctuation and remove tracking
    params (utm_*, ref) that Simplify appends, so the link is the raw ATS URL."""
    u = u.strip().strip("<>").strip()
    try:
        parsed = urlsplit(u)
    except ValueError:
        return u
    if not parsed.query:
        return u
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() != "ref"
    ]
    return urlunsplit(parsed._replace(query=urlencode(kept)))


# ---- date parsing ----------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(raw: str) -> date | None:
    if not raw:
        return None
    s = raw.strip().lower()

    # relative: "3d", "12d", "0d", "3 days ago"
    m = re.match(r"(\d+)\s*d", s)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    if "today" in s or s in {"0d", "new"}:
        return date.today()

    # "Jul 01" or "Jul 01 2027"
    m = re.match(r"([a-z]{3,4})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?", s)
    if m:
        mon = _MONTHS.get(m.group(1)[:3])
        if mon:
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else _infer_year(mon)
            try:
                return date(year, mon, day)
            except ValueError:
                return None

    # ISO-ish
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _infer_year(month: int) -> int:
    """Given a bare 'Jul 01', assume the most recent past occurrence."""
    today = date.today()
    year = today.year
    if month > today.month:
        year -= 1
    return year
