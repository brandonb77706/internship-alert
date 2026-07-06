"""Thin requests wrapper with sane defaults + logging."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("bot.http")


class Http:
    def __init__(self, cfg: dict):
        http_cfg = cfg.get("http", {})
        self.timeout = int(http_cfg.get("timeout_seconds", 25))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": http_cfg.get(
                    "user_agent", "internship-alert-bot/1.0"
                ),
                "Accept": "application/json, text/plain, */*",
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response | None:
        try:
            resp = self.session.get(url, timeout=self.timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("GET %s failed: %s", url, exc)
            return None

    def post(self, url: str, **kwargs) -> requests.Response | None:
        try:
            resp = self.session.post(url, timeout=self.timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("POST %s failed: %s", url, exc)
            return None
