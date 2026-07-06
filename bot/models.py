"""Core data model for a job posting."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace so trivial edits don't dedupe wrong."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:12]


@dataclass
class Job:
    """A single internship posting from any source."""

    company: str
    title: str
    url: str
    location: str = ""
    date_posted: str = ""          # human-readable string as found
    source: str = ""               # e.g. "SimplifyJobs" or "Greenhouse:DoorDash"
    posted_date: date | None = None  # parsed date, if we could parse one
    is_priority: bool = False

    def stable_id(self) -> str:
        """Deterministic id: company + normalized title + url hash.

        Stable across runs so we never re-email the same posting, but distinct
        enough that a genuinely new role at the same company still fires.
        """
        company = _normalize_title(self.company)
        title = _normalize_title(self.title)
        return f"{company}::{title}::{_url_hash(self.url)}"

    def to_state(self) -> dict:
        return {
            "id": self.stable_id(),
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "source": self.source,
        }
