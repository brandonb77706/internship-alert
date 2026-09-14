"""Config loading + logging setup."""

from __future__ import annotations

import logging
import os
import re
import sys

import yaml

log = logging.getLogger("bot")


def setup_logging(verbose: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml did not parse to a mapping")
    return cfg


def get_email_credentials(cfg: dict) -> dict:
    """Pull email secrets from the environment (GitHub Secrets in CI)."""
    raw = cfg.get("email", {}).get("recipient_override") or os.environ.get(
        "RECIPIENT_EMAIL", ""
    )
    return {
        "gmail_address": os.environ.get("GMAIL_ADDRESS", ""),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "recipients": _parse_recipients(raw),
    }


def _parse_recipients(raw) -> list[str]:
    """Accept a YAML list, or a comma/semicolon-separated string, of addresses."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[,;]", str(raw or ""))
    return [p.strip() for p in parts if p.strip()]
