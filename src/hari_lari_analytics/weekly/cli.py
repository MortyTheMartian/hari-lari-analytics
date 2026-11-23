"""`hari-lari-weekly`: extract, model, report, upload and deliver the weekly report."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from hari_lari_analytics.deliver import settings as secrets
from hari_lari_analytics.deliver.drive import DriveUploader, drive_session
from hari_lari_analytics.deliver.telegram import TelegramClient
from hari_lari_analytics.extract.cli import load_env, make_store
from hari_lari_analytics.extract.client import MokaClient
from hari_lari_analytics.extract.run import DEFAULT_SINCE, run_extraction
from hari_lari_analytics.extract.tokens import TokenManager
from hari_lari_analytics.weekly.log import JsonLog
from hari_lari_analytics.weekly.run import Services, Settings, run_weekly
from hari_lari_analytics.weekly.store import make_store as make_data_store


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="hari-lari-weekly", description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="do everything except sending; files go under dry-run/")  # fmt: skip
    p.add_argument("--force", action="store_true",
                   help="send even if this week was already sent")  # fmt: skip
    p.add_argument("--anchor", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="last day of the report (default: yesterday in Bali)")  # fmt: skip
    p.add_argument("--to", choices=["group", "admin"], default="group",
                   help="admin: a private run to the admin chat, not marked as sent")  # fmt: skip
    p.add_argument("--data-root", default=env("DATA_ROOT", "data/weekly"),
                   help="folder or gs://bucket[/prefix] for raw/, reports/, sent/")  # fmt: skip
    p.add_argument("--since", default=DEFAULT_SINCE, help="first day of the extraction")
    p.add_argument("--aliases", type=Path, default=Path("config/item_aliases.csv"))
    p.add_argument("--category-groups", type=Path, default=Path("config/category_group.csv"))
    p.add_argument("--drive-folder-id", default=env("DRIVE_FOLDER_ID"),
                   help="Drive folder to upload the week folders to")  # fmt: skip
    # Same options and environment variables as hari-lari-extract
    p.add_argument("--token-store", choices=["file", "secret"],
                   default=env("TOKEN_STORE", "file"))  # fmt: skip
    p.add_argument("--token-file", type=Path,
                   default=Path(env("MOKA_TOKEN_FILE", "extraction/moka/.token.json")))  # fmt: skip
    p.add_argument("--gcp-project", default=env("GCP_PROJECT"))
    p.add_argument("--token-secret", default=env("MOKA_TOKEN_SECRET", "moka-oauth-token"))
    return p


def build_services(args: argparse.Namespace, log: JsonLog) -> Services:
    telegram: TelegramClient | None = None

    def get_telegram() -> TelegramClient:
        nonlocal telegram
        if telegram is None:
            telegram = TelegramClient(secrets.get_secret(secrets.BOT_TOKEN, args.gcp_project))
        return telegram

    def chat_id(target: str) -> str:
        spec = secrets.ADMIN_CHAT_ID if target == "admin" else secrets.CHAT_ID
        return secrets.get_secret(spec, args.gcp_project)

    def extract(out: str):
        try:
            store = make_store(args)
        except SystemExit as error:  # a missing option; report it as a failed step
            raise RuntimeError(str(error)) from None
        client = MokaClient(TokenManager(store))
        return run_extraction(
            client, out, since=args.since, log=lambda m: log.info(m, step="extract")
        )

    def drive(folder_id: str) -> DriveUploader:
        get = lambda spec: secrets.get_secret(spec, args.gcp_project)  # noqa: E731
        session = drive_session(
            get(secrets.DRIVE_CLIENT_ID),
            get(secrets.DRIVE_CLIENT_SECRET),
            get(secrets.DRIVE_REFRESH_TOKEN),
        )
        return DriveUploader(session, folder_id)

    return Services(extract=extract, telegram=get_telegram, chat_id=chat_id, drive=drive)


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    log = JsonLog()
    settings = Settings(
        store=make_data_store(args.data_root),
        aliases=args.aliases,
        category_groups=args.category_groups,
        anchor=args.anchor,
        dry_run=args.dry_run,
        force=args.force,
        to=args.to,
        drive_folder_id=args.drive_folder_id,
    )
    return run_weekly(settings, build_services(args, log), log)


if __name__ == "__main__":
    raise SystemExit(main())
