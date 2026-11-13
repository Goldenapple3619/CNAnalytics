"""Delivery of newly created alerts: always logged, optionally POSTed to a webhook."""
from __future__ import annotations

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import Settings

log = logging.getLogger("alerts")

class Notifier:
    def __init__(self, settings: Settings):
        self.url = settings.webhook_url
        self.timeout = settings.webhook_timeout
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webhook") if self.url else None

    def notify(self, alerts: list[dict]) -> None:
        for alert in alerts:
            log.warning("[%s] %s - %s", alert["severity"].upper(), alert["title"], alert["message"])
            if self._pool is not None:
                self._pool.submit(self._post, alert)

    def _post(self, alert: dict) -> None:
        request = urllib.request.Request(
            self.url, data=json.dumps({"event": "alert.created", "alert": alert}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "processor/1"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except Exception as exc:  # delivery is best effort: the alert is stored either way
            log.error("webhook delivery failed for alert %s: %s", alert["id"], exc)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
