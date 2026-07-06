#!/usr/bin/env python3
"""Internship alert bot — entry point.

Usage:
    python main.py                 # normal run: fetch, diff, email new jobs
    python main.py --dry-run       # fetch + build email, print to console, send nothing
    python main.py --seed          # record everything as seen without emailing
    python main.py --config other.yaml

On the very first run (empty/missing state file) the bot auto-seeds: it records
every current posting as "seen" and sends NO email, so you don't get blasted
with hundreds of old jobs. After that, only genuinely new postings trigger mail.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot import emailer, filters, state
from bot.config import get_email_credentials, load_config, setup_logging
from bot.http import Http
from bot.models import Job
from bot.sources import ats, github_repos

log = logging.getLogger("bot.main")


def collect_jobs(http: Http, cfg: dict) -> list[Job]:
    """Gather jobs from every source. Each source is isolated: a failure in one
    is logged and skipped, never fatal."""
    jobs: list[Job] = []

    log.info("=== GitHub tracker repos ===")
    for repo_cfg in cfg.get("github_repos", []):
        try:
            jobs.extend(github_repos.fetch(http, repo_cfg))
        except Exception as exc:  # defensive: never let one repo kill the run
            log.warning("Repo %s failed: %s", repo_cfg.get("name"), exc)

    log.info("=== Company ATS feeds ===")
    for company in cfg.get("companies", []):
        if not company.get("enabled", False):
            continue
        try:
            jobs.extend(ats.fetch_company(http, company))
        except Exception as exc:
            log.warning("Company %s failed: %s", company.get("name"), exc)

    log.info("Collected %d raw jobs across all sources", len(jobs))
    return jobs


def dedupe_within_run(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        jid = job.stable_id()
        if jid in seen:
            continue
        seen.add(jid)
        out.append(job)
    if len(out) != len(jobs):
        log.info("De-duped %d duplicate rows within this run", len(jobs) - len(out))
    return out


def run(args) -> int:
    cfg = load_config(args.config)
    http = Http(cfg)
    state_path = cfg.get("state_file", "seen_jobs.json")

    # 1. Fetch everything
    raw = collect_jobs(http, cfg)
    raw = dedupe_within_run(raw)

    # 2. Filter to US/Remote Summer-2027 SWE-intern roles
    kept = filters.apply_filters(raw, cfg)
    emailer.mark_priorities(kept, cfg)

    # 3. Load dedupe state
    st = state.load_state(state_path)
    first_run = state.is_empty(st)

    # 4. Seeding path (first run OR explicit --seed): record, don't email
    if args.seed or first_run:
        why = "explicit --seed flag" if args.seed else "empty state (first run)"
        log.info("SEEDING mode (%s): recording %d jobs, sending no email",
                 why, len(kept))
        for job in kept:
            state.mark_seen(st, job)
        if args.dry_run:
            log.info("[dry-run] would seed %d jobs and write %s",
                     len(kept), state_path)
        else:
            state.save_state(state_path, st)
        print(f"Seeded {len(kept)} jobs. No email sent (this is expected on "
              f"the first run).")
        return 0

    # 5. Diff against seen
    new_jobs = [j for j in kept if not state.has_seen(st, j.stable_id())]
    log.info("Diff: %d new jobs (of %d that passed filters)", len(new_jobs), len(kept))

    if not new_jobs:
        log.info("No new jobs — sending nothing.")
        print("No new jobs. No email sent.")
        return 0

    # priority first, then company, then title — stable, readable ordering
    new_jobs.sort(key=lambda j: (not j.is_priority, j.company.lower(), j.title.lower()))

    subject = emailer.build_subject(new_jobs)
    html_body = emailer.build_html(new_jobs, cfg)

    if args.dry_run:
        _print_dry_run(subject, new_jobs, html_body)
        log.info("[dry-run] not sending, not updating state")
        return 0

    # 6. Send, then record as seen and persist
    creds = get_email_credentials(cfg)
    emailer.send_email(subject, html_body, creds, cfg)
    for job in new_jobs:
        state.mark_seen(st, job)
    state.save_state(state_path, st)
    print(f"Emailed {len(new_jobs)} new jobs and updated {state_path}.")
    return 0


def _print_dry_run(subject: str, jobs: list[Job], html_body: str) -> None:
    print("\n" + "=" * 70)
    print(f"SUBJECT: {subject}")
    print("=" * 70)
    for job in jobs:
        star = "⭐" if job.is_priority else "  "
        print(f"{star} {job.company} — {job.title}")
        print(f"     {job.location or 'Location N/A'} | {job.date_posted or 'n/a'} "
              f"| {job.source}")
        print(f"     Apply: {job.url}")
    print("=" * 70)
    try:
        with open("email_preview.html", "w", encoding="utf-8") as fh:
            fh.write(html_body)
        print("Full HTML preview written to email_preview.html")
    except OSError as exc:
        log.warning("Could not write email_preview.html: %s", exc)
    print(f"[dry-run] {len(jobs)} new jobs — nothing sent.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE internship alert bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the email to console instead of sending; "
                             "does not modify state.")
    parser.add_argument("--seed", action="store_true",
                        help="Record all current jobs as seen without emailing.")
    parser.add_argument("--quiet", action="store_true", help="Less logging.")
    args = parser.parse_args()

    setup_logging(verbose=not args.quiet)
    try:
        return run(args)
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
