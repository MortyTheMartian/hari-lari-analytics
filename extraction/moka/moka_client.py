"""Minimal Moka API client (stdlib only): .env loading, OAuth token handling, GET helper."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TOKEN_FILE = HERE / ".token.json"
BASE = "https://api.mokapos.com"
SCOPES = "profile report customer library transaction sales_type checkout checkout_api"


def load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def authorize_url() -> str:
    q = {
        "client_id": os.environ["MOKA_CLIENT_ID"],
        "redirect_uri": os.environ["MOKA_REDIRECT_URI"],
        "response_type": "code",
        "scope": SCOPES,
    }
    return f"{BASE}/oauth/authorize?{urllib.parse.urlencode(q)}"


def _post_token(payload: dict) -> dict:
    payload = {
        "client_id": os.environ["MOKA_CLIENT_ID"],
        "client_secret": os.environ["MOKA_CLIENT_SECRET"],
        **payload,
    }
    req = urllib.request.Request(
        f"{BASE}/oauth/token",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token request failed: {e.code} {e.read().decode()[:500]}")
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    TOKEN_FILE.chmod(0o600)
    return tok


def exchange_code(code: str) -> dict:
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.environ["MOKA_REDIRECT_URI"],
        }
    )


def access_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit("No token yet. Run: uv run python extraction/moka/auth.py url / exchange")
    tok = json.loads(TOKEN_FILE.read_text())
    expires = int(tok.get("created_at", 0)) + int(tok.get("expires_in", 0))
    if expires - time.time() < 3600 and tok.get("refresh_token"):
        tok = _post_token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return tok["access_token"]


def get(path: str, params: dict | None = None) -> tuple[int, object]:
    """GET a Moka endpoint. Returns (http_status, parsed_json_or_text)."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode()
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body
