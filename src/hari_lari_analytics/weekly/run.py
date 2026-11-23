"""The weekly job: extract, model, report, upload, deliver, with a sent-marker and alerts.

Every step is timed and logged as JSON. A failing step sends the admin alert, logs an ERROR
(which also triggers the Cloud Monitoring email) and ends the run with exit code 1, so Cloud
Run marks the execution as failed.

Two things never fail the run, because the report itself has been sent by then or can still
be sent without them: the Drive upload (the report goes out without the link, and the admin
chat is told) and writing the sent-marker (the admin chat is told).

A dry run does everything except sending: no Telegram message, no Drive upload, no marker,
and all files go under the `dry-run/` prefix. It does not send admin alerts either; the
failure shows in the exit code and the log.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from hari_lari_analytics.cli import prepare_lines, write_reports
from hari_lari_analytics.deliver.caption import build_caption
from hari_lari_analytics.deliver.drive import DriveUploader
from hari_lari_analytics.deliver.telegram import TelegramClient, scrub, send_alert, send_notice
from hari_lari_analytics.extract.run import BALI, RunResult
from hari_lari_analytics.load import load_aliases
from hari_lari_analytics.load_api import load_api_sales
from hari_lari_analytics.weekly.log import JsonLog
from hari_lari_analytics.weekly.store import Store

TOKEN_WARN_DAYS = 45


@dataclass
class Settings:
    store: Store
    aliases: Path
    category_groups: Path
    anchor: date | None = None  # default: yesterday in Bali
    dry_run: bool = False
    force: bool = False
    to: str = "group"  # "group" or "admin" (a private run)
    drive_folder_id: str | None = None
    token_warn_days: int = TOKEN_WARN_DAYS


@dataclass
class Services:
    """Everything that touches the outside world, so the tests can swap it."""

    extract: Callable[[str], RunResult]  # output location -> the finished run
    telegram: Callable[[], TelegramClient]
    chat_id: Callable[[str], str]  # "group" | "admin" -> chat id
    drive: Callable[[str], DriveUploader] | None = None  # folder id -> uploader
    now: Callable[[], datetime] = lambda: datetime.now(BALI)


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def token_expiry_notice(manifest: dict, now: datetime, warn_days: int = TOKEN_WARN_DAYS):
    """A reminder text when the Moka token expires within `warn_days`, else None (5.6)."""
    raw = manifest.get("token_expires_at")
    if not raw:
        return None
    expires = datetime.fromisoformat(raw)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=BALI)
    days = (expires - now).total_seconds() / 86400
    if days >= warn_days:
        return None
    when = f"{expires:%-d %b %Y}"
    if days < 0:
        return f"🔑 The Moka API token expired on {when}. Re-authorise it now (docs/runbook.md)."
    return (
        f"🔑 The Moka API token expires on {when} ({int(days)} days left). "
        "Re-authorise it before then (docs/runbook.md, “Re-authorising the Moka token”)."
    )


def week_folder_name(anchor: date) -> str:
    return f"{anchor:%Y-%m-%d} (week {anchor.isocalendar().week})"


class Runner:
    """Runs the steps: timing, logging and the alert on failure."""

    def __init__(self, settings: Settings, services: Services, log: JsonLog) -> None:
        self.settings, self.services, self.log = settings, services, log
        self.timings: dict[str, float] = {}

    @contextmanager
    def step(self, name: str, *, fatal: bool = True, consequence: str = "") -> Iterator[None]:
        """Time and log a step. A failure sends the admin alert and raises StepFailed.

        With `fatal=False` the failure is only a warning plus an admin notice (saying what
        the consequence is), and the run goes on.
        """
        self.log.info(f"{name}: start", step=name)
        started = time.monotonic()
        try:
            yield
        except Exception as error:
            seconds = round(time.monotonic() - started, 1)
            self.timings[name] = seconds
            message = scrub(str(error) or type(error).__name__)
            fields = {"step": name, "error_type": type(error).__name__, "seconds": seconds}
            if fatal:
                self.log.error(f"{name} failed: {message}", **fields)
                self.alert(name, error)
                raise StepFailed(name) from error
            self.notice(f"⚠️ Hari Lari weekly report: step {name} failed. {consequence}\n\n"
                        f"{type(error).__name__}: {message}"[:900], name)  # fmt: skip
            return
        seconds = round(time.monotonic() - started, 1)
        self.timings[name] = seconds
        self.log.info(f"{name}: done in {seconds}s", step=name, seconds=seconds)

    def alert(self, step: str, error: BaseException) -> None:
        if self.settings.dry_run:
            return
        try:
            send_alert(self.services.telegram(), self.services.chat_id("admin"), step, error)
        except Exception as inner:  # noqa: BLE001 - an alert must never hide the real error
            self.log.warning(f"could not send the admin alert: {scrub(str(inner))}", step=step)

    def notice(self, text: str, step: str) -> None:
        """A warning or reminder for the admin chat. Logged, and sent unless it is a dry run."""
        self.log.warning(text, step=step)
        if self.settings.dry_run:
            return
        try:
            send_notice(self.services.telegram(), self.services.chat_id("admin"), text)
        except Exception as inner:  # noqa: BLE001
            self.log.warning(f"could not send the admin notice: {scrub(str(inner))}", step=step)


def run_weekly(settings: Settings, services: Services, log: JsonLog | None = None) -> int:
    """Run the whole job. Returns the exit code: 0 for success (or already sent), 1 for failure."""
    log = log or JsonLog()
    anchor = settings.anchor or (services.now() - timedelta(days=1)).date()
    dry = settings.dry_run
    store = settings.store.child("dry-run") if dry else settings.store
    marker_key = f"sent/{anchor:%Y-%m-%d}.json"
    # A private run to the admin chat must not block the real send to the group, and the
    # other way round, so only the group send uses the marker.
    uses_marker = not dry and settings.to == "group"
    run = Runner(settings, services, log)
    started = time.monotonic()
    log.info(
        f"weekly report for {anchor}", step="start", anchor=str(anchor), dry_run=dry,
        force=settings.force, to=settings.to,
    )  # fmt: skip

    try:
        if uses_marker and not settings.force:
            with run.step("check"):
                already = store.exists(marker_key)
            if already:
                log.info(
                    f"The report for {anchor} was already sent (marker {marker_key}); "
                    "use --force to send it again",
                    step="check", already_sent=True,
                )  # fmt: skip
                return 0

        with run.step("extract"):
            extraction = services.extract(store.url("raw"))
        with run.step("token", fatal=False, consequence="The token reminder was skipped."):
            notice = token_expiry_notice(
                extraction.manifest, services.now(), settings.token_warn_days
            )
        if notice:
            run.notice(notice, "token")

        with run.step("model"):
            result_run, result = load_api_sales(extraction.location, load_aliases(settings.aliases))
            result.notes.extend(
                f"Extraction warning: {w}" for w in result_run.manifest.get("warnings", [])
            )
            lines, anchor = prepare_lines(result, settings.category_groups, anchor, source="api")

        with tempfile.TemporaryDirectory() as tmp:
            with run.step("report"):
                files = write_reports(
                    lines, result, [f"Moka API extraction {result_run.name}"], anchor, Path(tmp)
                )

            with run.step("upload"):
                for path in (files.pdf, files.cover_png, files.excel):
                    key = f"reports/{anchor:%Y-%m-%d}/{path.name}"
                    store.upload(key, path)
                    log.info(f"stored {store.url(key)}", step="upload")

            drive_url = _drive_step(run, anchor, files.pdf, files.excel)

            caption = build_caption(lines, anchor, drive_url)
            with run.step("deliver"):
                if dry:
                    log.info(
                        "dry run: nothing is sent. The caption would be:\n" + caption,
                        step="deliver",
                    )
                else:
                    services.telegram().send_report(
                        services.chat_id(settings.to), files.cover_png, files.pdf, caption
                    )
                    log.info(f"sent to the {settings.to} chat", step="deliver")

        if uses_marker:
            _write_marker(run, store, marker_key, anchor, drive_url, extraction.location)
    except StepFailed:
        log.error("weekly report failed", step="end", seconds_by_step=run.timings)
        return 1

    log.info(
        f"weekly report for {anchor} finished in {time.monotonic() - started:.1f}s",
        step="end", seconds_by_step=run.timings, dry_run=dry,
    )  # fmt: skip
    return 0


def _drive_step(run: Runner, anchor: date, pdf: Path, excel: Path) -> str | None:
    s, svc = run.settings, run.services
    if s.dry_run:
        run.log.info("dry run: no Drive upload", step="drive")
        return None
    if not s.drive_folder_id or svc.drive is None:
        run.log.warning(
            "No Drive folder is configured (DRIVE_FOLDER_ID): the report goes out without a link",
            step="drive",
        )
        return None
    url = None
    with run.step("drive", fatal=False, consequence="The report goes out without the Drive link."):
        url = svc.drive(s.drive_folder_id).upload_week(week_folder_name(anchor), [pdf, excel])
    return url


def _write_marker(
    run: Runner, store: Store, key: str, anchor: date, drive_url: str | None, location: str
) -> None:
    note = (
        f"The report for {anchor} was sent, but the sent-marker could not be written. "
        "Do not re-run without checking: a re-run would send it a second time."
    )
    with run.step("marker", fatal=False, consequence=note):
        payload = {
            "anchor": str(anchor),
            "sent_at": run.services.now().isoformat(timespec="seconds"),
            "to": run.settings.to,
            "drive_url": drive_url,
            "extraction": location,
        }
        store.write_text(key, json.dumps(payload, indent=2))
