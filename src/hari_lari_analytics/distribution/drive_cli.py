"""`hari-lari-drive`: one-time consent so the job can upload to Google Drive as you.

    hari-lari-drive auth-url              print the link to open and allow
    hari-lari-drive exchange "<URL>"      store the refresh token in Secret Manager
    hari-lari-drive whoami                which Google user the stored token acts as
    hari-lari-drive folder <folder id>    check that a folder can be reached

The OAuth client id and secret come from Secret Manager (`deploy/setup.sh drive-secrets`).
Nothing secret is printed or written to a file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from hari_lari_analytics.distribution import settings
from hari_lari_analytics.distribution.drive import API, SCOPES, TOKEN_URI, drive_session

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = (
    "http://localhost:8765"  # nothing listens here; you copy the address from the browser
)
REFRESH_TOKEN_SECRET = settings.DRIVE_REFRESH_TOKEN[1]


def auth_url(client_id: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # we want a refresh token
        "prompt": "consent",  # and Google must send it again even if you consented before
    }
    return f"{AUTH_URI}?{urllib.parse.urlencode(query)}"


def code_from(value: str) -> str:
    """The code from the address the browser ended up on (or the bare code)."""
    value = value.strip()
    if value.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
        if "error" in query:
            raise RuntimeError(f"Google answered: {query['error'][0]}")
        if "code" not in query:
            raise RuntimeError("That address has no ?code=... part")
        return query["code"][0]
    return value


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URI, data=body), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as error:
        detail = json.loads(error.read() or b"{}")
        raise RuntimeError(
            f"Google refused the code: {detail.get('error_description') or detail.get('error')}"
        ) from None


def store_refresh_token(token: str, project: str) -> None:
    """Add a new version of the secret through gcloud, so the token never touches a file."""
    out = subprocess.run(
        ["gcloud", "secrets", "versions", "add", REFRESH_TOKEN_SECRET, "--data-file=-",
         "--project", project],
        input=token, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    if out.returncode != 0:
        raise RuntimeError(
            f"Could not store the secret {REFRESH_TOKEN_SECRET} "
            f"(did you run deploy/setup.sh drive-secrets?): {out.stderr.strip()[-300:]}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hari-lari-drive", description=__doc__)
    p.add_argument("--gcp-project", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("auth-url")
    ex = sub.add_parser("exchange")
    ex.add_argument("redirected_url", help="the address the browser ended up on after you allowed")
    sub.add_parser("whoami")
    fo = sub.add_parser("folder")
    fo.add_argument("folder_id")
    args = p.parse_args(argv)
    project = args.gcp_project
    try:
        client_id = settings.get_secret(settings.DRIVE_CLIENT_ID, project)
        if args.command == "auth-url":
            print(auth_url(client_id))
            return 0
        client_secret = settings.get_secret(settings.DRIVE_CLIENT_SECRET, project)
        if args.command == "exchange":
            token = exchange_code(client_id, client_secret, code_from(args.redirected_url))
            if not token.get("refresh_token"):
                raise RuntimeError("Google sent no refresh token; run auth-url again and allow")
            store_refresh_token(token["refresh_token"], project or "hari-lari")
            print("Saved. Check it with: hari-lari-drive whoami")
            return 0
        session = drive_session(
            client_id, client_secret, settings.get_secret(settings.DRIVE_REFRESH_TOKEN, project)
        )
        if args.command == "whoami":
            about = session.request(
                "GET", "https://www.googleapis.com/drive/v3/about", params={"fields": "user"},
                timeout=60,
            )  # fmt: skip
            if about.status_code != 200:
                raise RuntimeError(f"Drive refused the token ({about.status_code})")
            user = about.json()["user"]
            print(f"Drive access works as {user['displayName']} <{user['emailAddress']}>")
        else:
            r = session.request(
                "GET", f"{API}/{args.folder_id}",
                params={"fields": "name,owners(emailAddress)", "supportsAllDrives": "true"},
                timeout=60,
            )  # fmt: skip
            if r.status_code != 200:
                raise RuntimeError(f"Cannot open the folder ({r.status_code})")
            info = r.json()
            print(f"Folder: {info['name']} (owner {info['owners'][0]['emailAddress']})")
    except (RuntimeError, settings.MissingSecret) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
