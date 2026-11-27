"""`hari-lari-extract`: pull the Moka API into Parquet, plus one-time OAuth helpers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from hari_lari_analytics.sources.moka import tokens
from hari_lari_analytics.sources.moka.client import MokaClient
from hari_lari_analytics.sources.moka.run import DEFAULT_SINCE, ExtractionFailed, run_extraction
from hari_lari_analytics.sources.moka.tokens import (
    FileTokenStore,
    SecretManagerTokenStore,
    TokenManager,
    TokenStore,
)

DEFAULT_TOKEN_FILE = Path("extraction/moka/.token.json")


def load_env(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from .env without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def make_store(args: argparse.Namespace) -> TokenStore:
    if args.token_store == "secret":
        if not args.gcp_project:
            raise SystemExit("--gcp-project (or GCP_PROJECT) is required for --token-store secret")
        return SecretManagerTokenStore(args.gcp_project, args.token_secret)
    return FileTokenStore(args.token_file)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hari-lari-extract", description=__doc__)
    p.add_argument("--token-store", choices=["file", "secret"],
                   default=os.environ.get("TOKEN_STORE", "file"))  # fmt: skip
    p.add_argument("--token-file", type=Path,
                   default=Path(os.environ.get("MOKA_TOKEN_FILE", DEFAULT_TOKEN_FILE)))  # fmt: skip
    p.add_argument("--gcp-project", default=os.environ.get("GCP_PROJECT"))
    p.add_argument(
        "--token-secret", default=os.environ.get("MOKA_TOKEN_SECRET", "moka-oauth-token")
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="extract the API into Parquet (default command)")
    r.add_argument(
        "--out",
        default=os.environ.get("EXTRACT_OUT", "data/landing"),
        help="the landing folder or gs://bucket/landing; the run goes to moka/<run date>/",
    )
    r.add_argument("--since", default=DEFAULT_SINCE, help="YYYY-MM-DD, Bali date")
    r.add_argument("--until", help="YYYY-MM-DD exclusive (default now)")
    r.add_argument("--only", choices=["transactions", "catalog"])

    a = sub.add_parser("auth", help="one-time OAuth setup")
    a.add_argument("step", choices=["url", "implicit-url", "exchange", "save-token"])
    a.add_argument("value", nargs="?", help="code/redirect URL for exchange/save-token")

    sub.add_parser("refresh-token", help="force one token refresh and report what changed")
    sub.add_parser("token-info", help="show token expiry (no secrets)")
    return p


def cmd_auth(args, store: TokenStore) -> int:
    if args.step == "url":
        print(tokens.authorize_url())
    elif args.step == "implicit-url":
        print(tokens.authorize_url("token"))
    elif args.step == "exchange":
        if not args.value:
            raise SystemExit("exchange needs the code or the redirected URL")
        code = args.value
        if code.startswith("http"):
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
        tok = tokens.exchange_code(code)
        tok.setdefault("created_at", int(time.time()))
        store.save(tok)
        print("OK, scopes:", tok.get("scope"), "| expires_in:", tok.get("expires_in"))
    else:
        if not args.value:
            raise SystemExit("save-token needs the full redirected URL")
        frag = urllib.parse.parse_qs(urllib.parse.urlparse(args.value).fragment)
        tok = {k: v[0] for k, v in frag.items()}
        tok.setdefault("created_at", int(time.time()))
        store.save(tok)
        print("Saved token, scope:", tok.get("scope"))
    return 0


def cmd_refresh(store: TokenStore) -> int:
    old = store.load()
    mgr = TokenManager(store, refresh_within_days=10_000)  # force
    new = mgr.ensure_fresh()
    print("refresh_token rotated:", new.get("refresh_token") != old.get("refresh_token"))
    print("access_token changed:", new.get("access_token") != old.get("access_token"))
    print("new expires_in (days):", int(new.get("expires_in", 0)) / 86400)
    print("new expiry:", time.strftime("%F", time.gmtime(tokens.expires_at(new))))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    store = make_store(args)
    try:
        if args.cmd == "auth":
            return cmd_auth(args, store)
        if args.cmd == "refresh-token":
            return cmd_refresh(store)
        if args.cmd == "token-info":
            tok = store.load()
            left = (tokens.expires_at(tok) - time.time()) / 86400
            day = time.strftime("%F", time.gmtime(tokens.expires_at(tok)))
            has_refresh = bool(tok.get("refresh_token"))
            print(f"expires {day} ({left:.0f} days left), has refresh token: {has_refresh}")
            return 0
        client = MokaClient(TokenManager(store))
        result = run_extraction(
            client, args.out, since=args.since, until=args.until, only=args.only
        )
        print(f"\nOK -> {result.location}\n{json.dumps(result.manifest, indent=2)}")
        for w in result.manifest["checks"]["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        return 0
    except ExtractionFailed as e:
        print(f"EXTRACTION FAILED: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
