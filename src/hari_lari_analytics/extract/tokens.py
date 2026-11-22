"""OAuth token handling: where the token lives (TokenStore) and when it is refreshed."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

BASE = "https://api.mokapos.com"
SCOPES = "profile report customer library transaction sales_type checkout checkout_api"
REFRESH_WITHIN_DAYS = 30
DAY = 86400


class TokenStore(Protocol):
    def load(self) -> dict: ...

    def save(self, token: dict) -> None: ...


class FileTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            raise RuntimeError(f"No token at {self.path}. Run: hari-lari-extract auth url")
        return json.loads(self.path.read_text())

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token, indent=2))
        self.path.chmod(0o600)


class SecretManagerTokenStore:
    """Token JSON in a Secret Manager secret. A save adds a new secret version."""

    def __init__(self, project: str, secret: str = "moka-oauth-token", client=None) -> None:
        if client is None:
            from google.cloud import secretmanager  # needs the `gcp` extra

            client = secretmanager.SecretManagerServiceClient()
        self.client = client
        self.parent = f"projects/{project}/secrets/{secret}"

    def load(self) -> dict:
        resp = self.client.access_secret_version(name=f"{self.parent}/versions/latest")
        return json.loads(resp.payload.data.decode())

    def save(self, token: dict) -> None:
        self.client.add_secret_version(
            parent=self.parent, payload={"data": json.dumps(token).encode()}
        )


def expires_at(token: dict) -> int:
    return int(token.get("created_at", 0)) + int(token.get("expires_in", 0))


class TokenManager:
    """Hands out access tokens; refreshes when fewer than 30 days remain.

    The check runs once per process (`ensure_fresh`), so a short-lived token can never
    cause a refresh on every request. `force_refresh` is for a 401.
    """

    def __init__(
        self,
        store: TokenStore,
        *,
        clock: Callable[[], float] = time.time,
        refresh_within_days: float = REFRESH_WITHIN_DAYS,
        refresher: Callable[[dict], dict] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.refresh_within = refresh_within_days * DAY
        self._refresher = refresher or _post_refresh
        self._token: dict | None = None
        self.refreshed = False

    def ensure_fresh(self) -> dict:
        if self._token is None:
            tok = self.store.load()
            if expires_at(tok) - self.clock() < self.refresh_within and tok.get("refresh_token"):
                tok = self._refresh(tok)
            self._token = tok
        return self._token

    def access_token(self) -> str:
        return self.ensure_fresh()["access_token"]

    def force_refresh(self) -> str:
        if self.refreshed:
            raise RuntimeError("Token already refreshed in this run; refusing to refresh again")
        self._token = self._refresh(self.ensure_fresh())
        return self._token["access_token"]

    def expires_at(self) -> int:
        return expires_at(self.ensure_fresh())

    def _refresh(self, old: dict) -> dict:
        new = self._refresher(old)
        new.setdefault("refresh_token", old.get("refresh_token"))  # keep it if not rotated
        new.setdefault("created_at", int(self.clock()))
        self.store.save(new)
        self.refreshed = True
        return new


def _client_credentials() -> dict:
    return {
        "client_id": os.environ["MOKA_CLIENT_ID"],
        "client_secret": os.environ["MOKA_CLIENT_SECRET"],
    }


def _post_token(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}/oauth/token",
        data=json.dumps({**_client_credentials(), **payload}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token request failed: {e.code} {e.read().decode()[:500]}") from e


def _post_refresh(old: dict) -> dict:
    return _post_token({"grant_type": "refresh_token", "refresh_token": old["refresh_token"]})


def authorize_url(response_type: str = "code") -> str:
    q = {
        "client_id": os.environ["MOKA_CLIENT_ID"],
        "redirect_uri": os.environ["MOKA_REDIRECT_URI"],
        "response_type": response_type,
        "scope": SCOPES,
    }
    return f"{BASE}/oauth/authorize?{urllib.parse.urlencode(q)}"


def exchange_code(code: str) -> dict:
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.environ["MOKA_REDIRECT_URI"],
        }
    )
