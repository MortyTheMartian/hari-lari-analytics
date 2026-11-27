"""Moka API client: GET with retry/backoff, 120 s timeouts and a request log."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime

from hari_lari_analytics.sources.moka.tokens import BASE, TokenManager

TIMEOUT = 120
RETRY_STATUSES = (0, 429, 500, 502, 503, 504)
LOG_COLUMNS = ["time", "path", "status", "bytes", "seconds", "retry_after"]


class MokaClient:
    def __init__(
        self,
        tokens: TokenManager,
        *,
        retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable | None = None,
    ) -> None:
        self.tokens = tokens
        self.retries = retries
        self.sleep = sleep
        self.opener = opener or urllib.request.urlopen
        self.log: list[list] = []

    def get(self, url_or_path: str, params: dict | None = None):
        url = url_or_path if url_or_path.startswith("http") else BASE + url_or_path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        if "access_token" in q:  # next_url carries an empty token; auth goes in the header
            q.pop("access_token")
            url = parsed._replace(query=urllib.parse.urlencode(q, doseq=True)).geturl()
        unauthorized_retried = False
        for attempt in range(self.retries):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.tokens.access_token()}"}
            )
            t0 = time.time()
            try:
                with self.opener(req, timeout=TIMEOUT) as r:
                    status, body, hdr = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                status, body, hdr = e.code, e.read(), e.headers
            except (urllib.error.URLError, http.client.HTTPException, OSError):  # drops, resets
                status, body, hdr = 0, b"", {}
            retry_after = hdr.get("Retry-After", "") if hdr else ""
            self.log.append(
                [
                    datetime.now().isoformat(timespec="seconds"),
                    parsed.path,
                    status,
                    len(body),
                    f"{time.time() - t0:.2f}",
                    retry_after,
                ]
            )
            if status == 200:
                return json.loads(body)
            if status == 401 and not unauthorized_retried:
                unauthorized_retried = True
                self.tokens.force_refresh()
                continue
            if status in RETRY_STATUSES:
                self.sleep(max(float(retry_after or 0), 2**attempt))
                continue
            raise RuntimeError(f"{status} {url}: {body[:300]!r}")
        raise RuntimeError(f"gave up after {self.retries} attempts: {url}")
