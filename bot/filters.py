"""Filtering logic: keep only US/Remote Summer-2027 SWE-intern roles."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from .models import Job

log = logging.getLogger("bot.filters")

# US state names + abbreviations, used to accept location strings like
# "New York, NY" that don't literally say "United States".
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington dc", "washington, dc",
}
_US_STATE_ABBRS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
# Common US tech-hub cities (helps feeds that give only a city).
_US_CITIES = {
    "new york", "san francisco", "seattle", "austin", "boston", "chicago",
    "atlanta", "denver", "los angeles", "san jose", "sunnyvale", "mountain view",
    "palo alto", "cupertino", "menlo park", "bellevue", "redmond", "dallas",
    "houston", "washington", "arlington", "mclean", "plano", "san diego",
    "portland", "raleigh", "durham", "pittsburgh", "philadelphia", "miami",
    "phoenix", "columbus", "minneapolis", "nashville", "charlotte", "detroit",
    "salt lake city", "san mateo", "santa clara", "irvine", "boston",
}


# Workday reports multi-site postings as "2 Locations" instead of naming them,
# which is not a place and must not be read as "not in the US".
_VAGUE_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)


def _word_alternation(words) -> re.Pattern | None:
    """Build a \\b-anchored alternation so 'intern' can't match 'internal'."""
    cleaned = [re.escape(w.lower().strip()) for w in words if str(w).strip()]
    if not cleaned:
        return None
    return re.compile(r"\b(?:" + "|".join(cleaned) + r")\b")


class Filters:
    def __init__(self, cfg: dict):
        self.include = [k.lower() for k in cfg.get("include_keywords", [])]
        self.exclude = [k.lower() for k in cfg.get("exclude_keywords", [])]
        self.target_season = [k.lower() for k in cfg.get("target_season_keywords", [])]
        self.reject_season = [k.lower() for k in cfg.get("reject_season_keywords", [])]
        self.location_allow = [k.lower() for k in cfg.get("location_allow", [])]
        self.recent_days = int(cfg.get("recent_days", 30))

        pair_cfg = cfg.get("title_pair_match", {}) or {}
        self.pair_enabled = bool(pair_cfg.get("enabled", False))
        self._intern_re = _word_alternation(pair_cfg.get("intern_words", []))
        self._tech_re = _word_alternation(pair_cfg.get("tech_words", []))

    # -- title -------------------------------------------------------------
    def title_matches(self, title: str) -> bool:
        t = title.lower()
        if not (any(k in t for k in self.include) or self._pair_matches(t)):
            return False
        if any(k in t for k in self.exclude):
            return False
        return True

    def _pair_matches(self, t: str) -> bool:
        """Fallback for titles that put the words in an unexpected order.

        Substring matching assumes big-tech phrasing ("Software Engineer
        Intern"). Plenty of employers write the same role as "Summer 2027
        Internship – Technology – Software Engineering", which no fixed phrase
        will ever catch. So also accept any title carrying both an intern word
        and a tech word.

        The intern words are matched on word boundaries specifically so
        "Internal Auditor" and "International" don't read as internships.
        """
        if not self.pair_enabled:
            return False
        if not self._intern_re or not self._tech_re:
            return False
        return bool(self._intern_re.search(t) and self._tech_re.search(t))

    # -- season ------------------------------------------------------------
    def season_ok(self, job: Job) -> bool:
        blob = f"{job.title} {job.location} {job.date_posted}".lower()
        # Reject explicit wrong seasons.
        if any(k in blob for k in self.reject_season):
            return False
        # Accept explicit target season.
        if any(k in blob for k in self.target_season):
            return True
        # No season mentioned: accept if recent (or recency gate disabled).
        if self.recent_days <= 0:
            return True
        if job.posted_date is None:
            return True  # undated feed — let it through, dedupe handles repeats
        cutoff = date.today() - timedelta(days=self.recent_days)
        return job.posted_date >= cutoff

    # -- location ----------------------------------------------------------
    def location_ok(self, location: str) -> bool:
        if not location or not location.strip():
            return True  # many feeds omit location; don't over-filter
        if _VAGUE_LOCATION.match(location):
            return True  # "2 Locations" tells us nothing — let the job through
        loc = location.lower()
        if any(k in loc for k in self.location_allow):
            return True
        # tokenise on commas / slashes / pipes and check state + city sets
        parts = re.split(r"[,/|•\n]+", loc)
        tokens = {p.strip() for p in parts if p.strip()}
        for tok in tokens:
            if tok in _US_STATES or tok in _US_CITIES:
                return True
            # trailing state abbreviation e.g. "austin tx"
            words = tok.split()
            if words and words[-1] in _US_STATE_ABBRS:
                return True
        return False

    # -- grad date (best effort) ------------------------------------------
    def grad_date_ok(self, job: Job) -> bool:
        """Drop roles that clearly require graduating before Dec 2027.

        Only acts when a 'graduat...' constraint with a year is parseable;
        otherwise passes.
        """
        blob = f"{job.title} {job.date_posted}".lower()
        if "graduat" not in blob:
            return True
        years = re.findall(r"20(2[0-9])", blob)
        if not years:
            return True
        # If it mentions only years <= 2026, it's likely a full-time/early grad.
        yrs = {2000 + int(y) for y in years}
        if all(y <= 2026 for y in yrs):
            return False
        return True

    # -- combined ----------------------------------------------------------
    def keep(self, job: Job) -> bool:
        if not self.title_matches(job.title):
            return False
        if not self.location_ok(job.location):
            return False
        if not self.season_ok(job):
            return False
        if not self.grad_date_ok(job):
            return False
        return True


def apply_filters(jobs: list[Job], cfg: dict) -> list[Job]:
    f = Filters(cfg)
    kept = [j for j in jobs if f.keep(j)]
    log.info("Filter: %d/%d jobs passed", len(kept), len(jobs))
    return kept
