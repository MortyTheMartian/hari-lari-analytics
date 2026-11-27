"""Running a report (plan 9.7): `hari-lari-report run`, the job behind every scheduled report.

`run_report_job` checks the sent-marker, extracts (unless an existing run is named), sends the
Moka token reminder, builds the models and runs the data checks, builds the outputs, stores them,
evaluates `deliver.when`, uploads to Drive, sends to Telegram and writes the marker. Each step is
timed and logged as JSON; a failing step sends the admin alert, logs an ERROR (which also
triggers the Cloud Monitoring email) and ends the run with exit code 1.

Two things never fail the run, because the report has been sent by then or can still be sent
without them: the Drive upload (the text goes out without its lines that use the link, and the
admin chat is told) and writing the sent-marker (the admin chat is told).

A dry run does everything except sending: no Telegram message, no Drive upload, no marker and no
admin alert, and every file goes under the `dry-run/` prefix. `--to <target>` sends every
Telegram delivery to that one chat, and reads and writes no marker.

Layout under the data root (local or `gs://`): `landing/moka/<run date>/` (the extraction),
`reports/<report>/<anchor>/` (the outputs) and `sent/<report>/<anchor>.json` (the marker).
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.core.landing import LANDING
from hari_lari_analytics.core.log import JsonLog
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import Store
from hari_lari_analytics.distribution.drive import DriveUploader
from hari_lari_analytics.distribution.telegram import TelegramClient, scrub, send_alert, send_notice
from hari_lari_analytics.presentation.builder import BuildResult, build_report
from hari_lari_analytics.presentation.facets import Facet, run_facets
from hari_lari_analytics.presentation.outputs.telegram import fill_drive_url
from hari_lari_analytics.presentation.reports import (
    Report,
    TelegramDelivery,
    context,
    render,
    resolve_anchor,
)
from hari_lari_analytics.sources.moka.run import RunResult
from hari_lari_analytics.warehouse.checks import CheckReport
from hari_lari_analytics.warehouse.db import open_run

TOKEN_WARN_DAYS = 45

# The marker `hari-lari-weekly` wrote before the cut-over, so a week it sent is not sent again
LEGACY_MARKERS = {"weekly": "sent/{anchor}.json"}


@dataclass
class RunSettings:
    store: Store
    config: str = "config"  # a folder or gs:// (core/config.py)
    anchor: date | None = None  # default: the report's `anchor` parameter, else project.yml
    dry_run: bool = False
    force: bool = False
    to: str | None = None  # every Telegram delivery to this target instead (a private check)
    run: str | None = None  # an existing run (folder or gs://) instead of a new extraction
    drive_folder_id: str | None = None
    token_warn_days: int = TOKEN_WARN_DAYS


@dataclass
class Services:
    """Everything that touches the outside world, so the tests can swap it."""

    extract: Callable[[str], RunResult]  # output location -> the finished run
    telegram: Callable[[], TelegramClient]
    chat_id: Callable[[str], str]  # a target of project.yml -> its chat id
    drive: Callable[[str], DriveUploader] | None = None  # folder id -> uploader
    now: Callable[[], datetime] = lambda: project().now()


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def token_expiry_notice(manifest: dict, now: datetime, warn_days: int = TOKEN_WARN_DAYS):
    """A reminder text when the Moka token expires within `warn_days`, else None (5.6)."""
    raw = manifest.get("source_info", {}).get("token_expires_at") or manifest.get(
        "token_expires_at"
    )  # the second: a manifest written before the landing zone (t12)
    if not raw:
        return None
    expires = datetime.fromisoformat(raw)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=project().tz)
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


def marker_key(report_id: str, anchor: date) -> str:
    return f"sent/{report_id}/{anchor:%Y-%m-%d}.json"


def marker_keys(report_id: str, anchor: date) -> list[str]:
    """The markers that say a report was sent for an anchor: its own, and the old one."""
    keys = [marker_key(report_id, anchor)]
    if report_id in LEGACY_MARKERS:
        keys.append(LEGACY_MARKERS[report_id].format(anchor=f"{anchor:%Y-%m-%d}"))
    return keys


class Runner:
    """Runs the steps: timing, logging and the alert on failure."""

    def __init__(
        self, settings: RunSettings, services: Services, log: JsonLog, subject: str
    ) -> None:
        self.settings, self.services, self.log = settings, services, log
        self.subject = subject  # "report weekly", as the alerts name it
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
            self.notice(f"⚠️ Hari Lari {self.subject}: step {name} failed. {consequence}\n\n"
                        f"{type(error).__name__}: {message}"[:900], name)  # fmt: skip
            return
        seconds = round(time.monotonic() - started, 1)
        self.timings[name] = seconds
        self.log.info(f"{name}: done in {seconds}s", step=name, seconds=seconds)

    def alert(self, step: str, error: BaseException) -> None:
        if self.settings.dry_run:
            return
        try:
            send_alert(
                self.services.telegram(),
                self.services.chat_id("admin"),
                step,
                error,
                subject=self.subject,
            )
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


def data_store(settings: RunSettings) -> Store:
    return settings.store.child("dry-run") if settings.dry_run else settings.store


def extract_step(run: Runner, store: Store) -> str:
    """Extract into `landing/` of the store and send the token reminder; returns the run's
    location."""
    with run.step("extract"):
        extraction = run.services.extract(store.url(LANDING))
    with run.step("token", fatal=False, consequence="The token reminder was skipped."):
        notice = token_expiry_notice(
            extraction.manifest, run.services.now(), run.settings.token_warn_days
        )
    if notice:
        run.notice(notice, "token")
    return extraction.location


# --- one report -----------------------------------------------------------------------------------


def run_report_job(
    report: Report, settings: RunSettings, services: Services, log: JsonLog | None = None
) -> int:
    """Run one report. Returns the exit code: 0 for success (or already sent), 1 for failure."""
    log = log or JsonLog()
    anchor = resolve_anchor(report, settings.anchor, services.now())
    dry = settings.dry_run
    store = data_store(settings)
    # A private run to one chat must not block the real send, and the other way round
    uses_marker = not dry and settings.to is None
    run = Runner(settings, services, log, f"report {report.id}")
    started = time.monotonic()
    log.info(
        f"report {report.id} for {anchor}", step="start", report=report.id, anchor=str(anchor),
        dry_run=dry, force=settings.force, to=settings.to,
    )  # fmt: skip

    try:
        if uses_marker and not settings.force:
            with run.step("check"):
                found = next((k for k in marker_keys(report.id, anchor) if store.exists(k)), None)
            if found:
                log.info(
                    f"The report {report.id} for {anchor} was already sent (marker {found}); "
                    "use --force to send it again",
                    step="check", already_sent=True, report=report.id,
                )  # fmt: skip
                return 0

        location = settings.run or extract_step(run, store)

        with run.step("model"):
            db = open_run(location, settings.config, anchor)
            ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
        for warning in db.checks.warnings:
            log.warning(f"data check: {warning.describe()}", step="model")

        with tempfile.TemporaryDirectory() as tmp:
            with run.step("report"):
                built = build_report(report, ctx, Path(tmp))
            for overflow in built.overflows:
                log.warning(f"text overflow: {overflow.describe()}", step="report")

            prefix = f"reports/{report.id}/{anchor:%Y-%m-%d}"
            with run.step("upload"):
                for path in built.files:
                    store.upload(f"{prefix}/{path.name}", path)
                log.info(f"stored {len(built.files)} files in {store.url(prefix)}", step="upload")

            held = conditions(report, built, ctx, db.checks)
            if held and not any(ok for _, ok in held):
                log.info(
                    f"no condition of deliver.when holds ({describe(held)}): nothing is sent",
                    step="when", report=report.id,
                )  # fmt: skip
                return _finished(log, report, anchor, run, started)
            if held:
                log.info(f"deliver.when: {describe(held)}", step="when")

            drive_url = _drive_step(run, report, built, anchor)
            with run.step("deliver"):
                for delivery in report.spec.deliver.telegram:
                    _send(run, delivery, built, drive_url)

        if uses_marker:
            _write_marker(run, store, report, anchor, drive_url, location)
    except StepFailed:
        log.error(
            f"report {report.id} failed", step="end", report=report.id,
            seconds_by_step=run.timings,
        )  # fmt: skip
        return 1
    return _finished(log, report, anchor, run, started)


def _finished(log: JsonLog, report: Report, anchor: date, run: Runner, started: float) -> int:
    log.info(
        f"report {report.id} for {anchor} finished in {time.monotonic() - started:.1f}s",
        step="end", report=report.id, seconds_by_step=run.timings, dry_run=run.settings.dry_run,
    )  # fmt: skip
    return 0


# --- deliver.when ---------------------------------------------------------------------------------


def _component_facets(report: Report, built: BuildResult, ctx: QueryContext, name: str):
    """The facets of a component in this build: from its slots and sheets, else run once with
    the report's parameters (a component only in a text block)."""
    found: list[Facet] = []
    for page in built.run.pages:
        for slot in page.slots:
            if slot.slot.ref.bound.id == name:
                found += slot.facets
    for sheet in built.run.sheets:
        if sheet.ref.bound.id == name:
            found += sheet.facets
    if found:
        return found
    return run_facets(report.components[name], ctx, dict(report.params))


def _regular_rows(facet: Facet) -> int:
    rows = facet.result.rows
    if "is_other" in rows:
        rows = rows[~rows["is_other"].fillna(False).astype(bool)]
    return len(rows)


def conditions(
    report: Report, built: BuildResult, ctx: QueryContext, checks: CheckReport
) -> list[tuple[str, bool]]:
    """Each condition of `deliver.when` with whether it holds; empty when there is none."""
    held: list[tuple[str, bool]] = []
    for condition in report.spec.deliver.when:
        if condition.insight is not None:
            ok = any(
                i.rule == condition.insight
                for page in built.run.pages
                for slot in page.slots
                for i in slot.candidates
            )
            held.append((f"insight {condition.insight}", ok))
        elif condition.rows is not None:
            facets = _component_facets(report, built, ctx, condition.rows)
            held.append((f"rows {condition.rows}", any(_regular_rows(f) for f in facets)))
        else:
            held.append(("checks warn", bool(checks.warnings)))
    return held


def describe(held: Sequence[tuple[str, bool]]) -> str:
    return ", ".join(f"{name}: {'yes' if ok else 'no'}" for name, ok in held)


# --- Drive and Telegram ---------------------------------------------------------------------------


def _drive_step(run: Runner, report: Report, built: BuildResult, anchor: date) -> str | None:
    spec, s, svc = report.spec.deliver.drive, run.settings, run.services
    if spec is None:
        return None
    if s.dry_run:
        run.log.info("dry run: no Drive upload", step="drive")
        return None
    if not s.drive_folder_id or svc.drive is None:
        run.log.warning(
            "No Drive folder is configured (DRIVE_FOLDER_ID): the report goes out without a link",
            step="drive",
        )
        return None
    folder = render(
        spec.folder, context(report, anchor, report.params), f"{report.source}: deliver.drive"
    )
    by_kind = {"pdf": built.pdf, "xlsx": built.xlsx, "json": built.bundle}
    files = [by_kind[kind] for kind in spec.files if by_kind[kind] is not None]
    url = None
    with run.step("drive", fatal=False, consequence="The report goes out without the Drive link."):
        url = svc.drive(s.drive_folder_id).upload_week(folder, files)
    return url


def _photo(built: BuildResult, page: str) -> Path:
    """The PNG of a page of `outputs.png`; a repeated page's base id means its first repeat."""
    if page in built.pngs:
        return built.pngs[page]
    for run in built.run.pages:
        if run.page.base == page and run.page.id in built.pngs:
            return built.pngs[run.page.id]
    raise ValueError(f"no PNG of page {page} in this build")


def _send(run: Runner, delivery: TelegramDelivery, built: BuildResult, drive_url: str | None):
    target = run.settings.to or delivery.to
    text = fill_drive_url(built.run.texts[delivery.text], drive_url) if delivery.text else ""
    photo = _photo(built, delivery.photo) if delivery.photo else None
    document = None
    if delivery.document:
        document = built.pdf if delivery.document == "pdf" else built.xlsx
        if document is None:
            raise ValueError(f"the build has no {delivery.document} to send")
    sent = [p.name for p in (photo, document) if p is not None]
    if run.settings.dry_run:
        run.log.info(
            f"dry run: nothing is sent. To {target}: {', '.join(sent) or 'a message'}, "
            f"with the text:\n{text}",
            step="deliver", target=target,
        )  # fmt: skip
        return
    client, chat = run.services.telegram(), run.services.chat_id(target)
    if photo is not None:
        client.send_photo(chat, photo, text)
        if document is not None:
            client.send_document(chat, document)
    elif document is not None:
        client.send_document(chat, document, text)
    else:
        client.send_message(chat, text)
    run.log.info(f"sent {', '.join(sent) or 'a message'} to the {target} chat", step="deliver")


def _write_marker(
    run: Runner, store: Store, report: Report, anchor: date, drive_url: str | None, location: str
) -> None:
    key = marker_key(report.id, anchor)
    note = (
        f"The report {report.id} for {anchor} was sent, but the sent-marker could not be "
        "written. Do not re-run without checking: a re-run would send it a second time."
    )
    with run.step("marker", fatal=False, consequence=note):
        payload = {
            "report": report.id,
            "anchor": str(anchor),
            "sent_at": run.services.now().isoformat(timespec="seconds"),
            "to": sorted({d.to for d in report.spec.deliver.telegram}),
            "drive_url": drive_url,
            "extraction": location,
        }
        store.write_text(key, json.dumps(payload, indent=2))


# --- every scheduled report -----------------------------------------------------------------------


def run_scheduled(
    reports: Sequence[Report], settings: RunSettings, services: Services, log: JsonLog | None = None
) -> int:
    """Run every report that has a schedule, one after the other on one extraction. Returns 1
    if any of them failed. The release's dry run uses this, so every scheduled report must
    build before a release is promoted."""
    log = log or JsonLog()
    scheduled = [r for r in sorted(reports, key=lambda r: r.id) if r.spec.schedule is not None]
    log.info(
        f"{len(scheduled)} scheduled report(s): {', '.join(r.id for r in scheduled) or 'none'}",
        step="start", dry_run=settings.dry_run,
    )  # fmt: skip
    if not scheduled:
        return 0
    location = settings.run
    if location is None:
        run = Runner(settings, services, log, "scheduled reports")
        try:
            location = extract_step(run, data_store(settings))
        except StepFailed:
            log.error("scheduled reports failed", step="end", seconds_by_step=run.timings)
            return 1
    shared = replace(settings, run=location)
    failed = [r.id for r in scheduled if run_report_job(r, shared, services, log) != 0]
    if failed:
        log.error(f"scheduled report(s) failed: {', '.join(failed)}", step="end")
        return 1
    return 0
