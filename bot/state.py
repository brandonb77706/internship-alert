"""Dedupe state: the set of job ids we've already emailed."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot.state")


def load_state(path: str) -> dict:
    """Return the state dict. Missing/empty/corrupt file => fresh empty state."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        log.info("State file %s missing or empty — treating as first run", path)
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("State file unreadable (%s) — starting fresh", exc)
        return _empty_state()

    if not isinstance(data, dict) or "seen" not in data:
        log.warning("State file has unexpected shape — starting fresh")
        return _empty_state()
    return data


def _empty_state() -> dict:
    return {"seen": {}, "created": _now(), "updated": _now()}


def is_empty(state: dict) -> bool:
    return not state.get("seen")


def has_seen(state: dict, job_id: str) -> bool:
    return job_id in state.get("seen", {})


def mark_seen(state: dict, job) -> None:
    state.setdefault("seen", {})[job.stable_id()] = {
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "source": job.source,
        "first_seen": _now(),
    }


def save_state(path: str, state: dict) -> None:
    state["updated"] = _now()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    log.info("Saved state: %d seen jobs -> %s", len(state.get("seen", {})), path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
