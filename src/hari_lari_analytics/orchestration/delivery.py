"""Running a report (plan 9.7): `hari-lari-report run`, the job behind every scheduled report.

`run_report_job` checks whether the report was already sent (the ledger, or an old
sent-marker), extracts and builds the warehouse (unless a warehouse is named), sends the Moka
token reminder, opens the warehouse read-only, builds the outputs, stores them, evaluates
`deliver.when`, uploads to Drive, sends to Telegram, writes the marker and regenerates
`reports/index.json`. Each step is timed and logged as JSON; a failing step sends the admin
alert, logs an ERROR (which also triggers the Cloud Monitoring email) and ends the run with
exit code 1. The extraction, the warehouse and the report each write a ledger record
(`ledger.py`); the marker is still written, so an image from before the ledger, after a
rollback, does not send a report twice.

Two things never fail the run, because the report has been sent by then or can still be sent
without them: the Drive upload (the text goes out without its lines that use the link, and the
admin chat is told) and writing the sent-marker (the admin chat is told).

A dry run does everything except sending: no Telegram message, no Drive upload, no marker and no
admin alert, and every file goes under the `dry-run/` prefix. `--to <target>` sends every
Telegram delivery to that one chat, and reads and writes no marker.

Layout under the data root (local or `gs://`): `landing/moka/<run date>/` (the extraction),
`warehouse/<run date>/` (the models), `reports/<report>/<anchor>/` (the outputs),
`runs/<run date>/` (the ledger) and `sent/<report>/<anchor>.json` (the marker).
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
from typing import Literal

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.core.landing import LANDING, image_tag, new_run_id
from hari_lari_analytics.core.log import JsonLog
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import Store
from hari_lari_analytics.distribution.drive import DriveUploader
from hari_lari_analytics.distribution.telegram import TelegramClient, scrub, send_alert, send_notice
from hari_lari_analytics.orchestration.ledger import (
    Delivery,
    Record,
    delivered_targets,
    sent_record,
    write_record,
)
from hari_lari_analytics.orchestration.report_index import write_index
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
from hari_lari_analytics.warehouse.build import (
    WAREHOUSE,
    BuildInfo,
    build_warehouse,
    local_warehouse,
    open_warehouse,
    warehouse_url,
)
from hari_lari_analytics.warehouse.checks import CheckReport

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
    # an existing warehouse (a run date, or a path or gs:// URL) instead of extract and build
    warehouse: str | None = None
    drive_folder_id: str | None = None
    token_warn_days: int = TOKEN_WARN_DAYS
    trigger: Literal["scheduler", "manual"] = "manual"  # the ledger's `trigger`
    run_id: str | None = None  # the run the steps belong to (a tick's); default: a new one


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
    """Runs the steps of one ledger record: timing, logging, the alert on failure, and the
    record itself (`ledger.py`)."""

    def __init__(
        self,
        settings: RunSettings,
        services: Services,
        log: JsonLog,
        subject: str,
        note: str | None = None,
    ) -> None:
        self.settings, self.services, self.log = settings, services, log
        self.subject = subject  # "report weekly", as the alerts name it
        self.note = note  # a line for the alert, such as when the next tick retries
        self.timings: dict[str, float] = {}
        self.started_at = services.now()
        self.started = time.monotonic()
        self.run_id = settings.run_id or new_run_id(self.started_at)
        self.run_date = project().today(self.started_at)
        self.deliveries: list[Delivery] = []
        self.warnings: list[str] = []
        self.error: str | None = None

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
                self.error = f"{type(error).__name__}: {message}"[:900]
                self.alert(name, error)
                raise StepFailed(name) from error
            self.warnings.append(f"step {name} failed: {consequence}")
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
                note=self.note,
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

    def record(
        self,
        step: str,
        status: str,
        *,
        reason: str | None = None,
        inputs: dict | None = None,
        outputs: list[str] | None = None,
    ) -> Record | None:
        """Write the ledger record of this step. A failure to write it is an admin notice,
        never a failed run: the step's work is done by then."""
        s = self.settings
        record = Record(
            run_id=self.run_id, run_date=self.run_date, step=step, trigger=s.trigger,
            image=image_tag(), dry_run=s.dry_run, force=s.force, to=s.to, status=status,
            reason=reason, started_at=self.started_at, finished_at=self.services.now(),
            seconds=round(time.monotonic() - self.started, 1),
            seconds_by_phase=dict(self.timings), inputs=inputs or {}, outputs=outputs or [],
            deliveries=self.deliveries, warnings=self.warnings,
            error=self.error if status in ("failed", "skipped") else None,
        )  # fmt: skip
        try:
            write_record(data_store(s), record)
        except Exception as error:  # noqa: BLE001
            self.notice(
                f"⚠️ Hari Lari {self.subject}: the ledger record {record.key} could not be "
                f"written. {type(error).__name__}: {scrub(str(error))}"[:900],
                "ledger",
            )
        return record


def data_store(settings: RunSettings) -> Store:
    return settings.store.child("dry-run") if settings.dry_run else settings.store


# --- extract and warehouse ------------------------------------------------------------------------


def extract_step(
    settings: RunSettings,
    services: Services,
    log: JsonLog,
    subject: str,
    source: str = "moka",
    note: str | None = None,
) -> RunResult:
    """Extract one source into `landing/` of the data store, send the token reminder, and
    record `extract-<source>`. Raises StepFailed; `note` goes into its alert."""
    run = Runner(settings, services, log, subject, note)
    try:
        with run.step("extract"):
            extraction = services.extract(data_store(settings).url(LANDING))
    except StepFailed:
        run.record(f"extract-{source}", "failed")
        raise
    manifest = extraction.manifest
    with run.step("token", fatal=False, consequence="The token reminder was skipped."):
        notice = token_expiry_notice(manifest, services.now(), settings.token_warn_days)
    if notice:
        run.notice(notice, "token")
    run.warnings += list(manifest.get("checks", {}).get("warnings", []))
    landed = f"{LANDING}/{source}/{manifest['run_date']}" if "run_date" in manifest else None
    run.record(
        f"extract-{source}", "ok", inputs={"window": manifest.get("window")},
        outputs=[landed] if landed else [],
    )  # fmt: skip
    return extraction


def warehouse_step(
    settings: RunSettings, services: Services, log: JsonLog, subject: str, note: str | None = None
) -> BuildInfo:
    """Build the warehouse of the run date in the data store and record `warehouse`. Raises
    StepFailed (a missing config file, a failing error check); `note` goes into its alert."""
    run = Runner(settings, services, log, subject, note)
    try:
        with run.step("warehouse"):
            info = build_warehouse(
                data_store(settings), run.run_date, settings.config, now=services.now()
            )
    except StepFailed:
        run.record("warehouse", "failed")
        raise
    run.warnings += [f"data check: {w}" for w in info.checks.warnings]
    marts = [f"{WAREHOUSE}/{info.date}/marts/{m}.parquet" for m in info.marts]
    run.record(
        "warehouse", "ok",
        inputs={
            "landing": {s: f"{r.run_date}" for s, r in info.landing.items()},
            "config": info.config,
        },
        outputs=[info.database, *marts],
    )  # fmt: skip
    return info


def prepare(settings: RunSettings, services: Services, log: JsonLog, subject: str) -> str:
    """Extract and build the warehouse; returns its URL. Raises StepFailed (the failed step
    has its ledger record and its alert)."""
    extract_step(settings, services, log, subject)
    info = warehouse_step(settings, services, log, subject)
    return data_store(settings).url(info.database)


# --- one report -----------------------------------------------------------------------------------


def run_report_job(
    report: Report, settings: RunSettings, services: Services, log: JsonLog | None = None
) -> int:
    """Run one report. Returns the exit code: 0 for success (or already sent), 1 for failure.

    Without `settings.warehouse` it extracts and builds the warehouse first; a failure there
    records the report as skipped."""
    log = log or JsonLog()
    anchor = resolve_anchor(report, settings.anchor, services.now())
    dry = settings.dry_run
    store = data_store(settings)
    # A private run to one chat must not block the real send, and the other way round
    uses_marker = not dry and settings.to is None
    subject = f"report {report.id}"
    log.info(
        f"report {report.id} for {anchor}", step="start", report=report.id, anchor=str(anchor),
        dry_run=dry, force=settings.force, to=settings.to,
    )  # fmt: skip
    run = Runner(settings, services, log, subject)
    inputs: dict = {"report": report.id, "anchor": f"{anchor:%Y-%m-%d}"}
    outputs: list[str] = []
    try:
        if uses_marker and not settings.force:
            with run.step("check"):
                found = already_sent(store, report.id, anchor)
            if found:
                log.info(
                    f"The report {report.id} for {anchor} was already sent ({found}); "
                    "use --force to send it again",
                    step="check", already_sent=True, report=report.id,
                )  # fmt: skip
                return 0
        if settings.warehouse is None:
            try:
                location = prepare(replace(settings, run_id=run.run_id), services, log, subject)
            except StepFailed as failed:
                run.error = f"step {failed.step} failed"
                run.record(
                    f"report-{report.id}", "skipped", reason=f"step {failed.step} failed",
                    inputs=inputs,
                )  # fmt: skip
                log.error(f"report {report.id} failed", step="end", report=report.id)
                return 1
            run.settings = replace(settings, warehouse=location)
        status = _report_steps(run, report, anchor, inputs, outputs)
    except StepFailed:
        run.record(f"report-{report.id}", "failed", inputs=inputs, outputs=outputs)
        _write_index(run)
        log.error(
            f"report {report.id} failed", step="end", report=report.id,
            seconds_by_step=run.timings,
        )  # fmt: skip
        return 1
    run.record(f"report-{report.id}", status, inputs=inputs, outputs=outputs)
    _write_index(run)
    log.info(
        f"report {report.id} for {anchor} finished in {time.monotonic() - run.started:.1f}s",
        step="end", report=report.id, status=status, seconds_by_step=run.timings, dry_run=dry,
    )  # fmt: skip
    return 0


def already_sent(store: Store, report_id: str, anchor: date) -> str | None:
    """What says the report went out for the anchor: a ledger record or an old sent-marker."""
    record = sent_record(store, report_id, anchor)
    if record is not None:
        return f"ledger {record.key}"
    found = next((k for k in marker_keys(report_id, anchor) if store.exists(k)), None)
    return f"marker {found}" if found else None


def _report_steps(
    run: Runner, report: Report, anchor: date, inputs: dict, outputs: list[str]
) -> str:
    """Open the warehouse, build, store and deliver; returns the status (`sent` or `quiet`).
    Raises StepFailed."""
    settings, log = run.settings, run.log
    store = data_store(settings)
    uses_marker = not settings.dry_run and settings.to is None
    with tempfile.TemporaryDirectory() as tmp:
        with run.step("model"):
            location = warehouse_url(settings.store, settings.warehouse)
            db = open_warehouse(local_warehouse(location, Path(tmp)), anchor)
            ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
            inputs["warehouse"] = location
            inputs["landing"] = dict(
                db.con.execute(
                    "select source, strftime(run_date, '%Y-%m-%d') from warehouse_landing"
                ).fetchall()
            )
        for warning in db.checks.warnings:
            log.warning(f"data check: {warning.describe()}", step="model")
            run.warnings.append(f"data check: {warning.describe()}")
        with run.step("report"):
            built = build_report(report, ctx, Path(tmp))
        for overflow in built.overflows:
            log.warning(f"text overflow: {overflow.describe()}", step="report")
            run.warnings.append(f"text overflow: {overflow.describe()}")

        prefix = f"reports/{report.id}/{anchor:%Y-%m-%d}"
        with run.step("upload"):
            for path in built.files:
                store.upload(f"{prefix}/{path.name}", path)
                outputs.append(f"{prefix}/{path.name}")
            log.info(f"stored {len(built.files)} files in {store.url(prefix)}", step="upload")

        held = conditions(report, built, ctx, db.checks)
        if held and not any(ok for _, ok in held):
            log.info(
                f"no condition of deliver.when holds ({describe(held)}): nothing is sent",
                step="when", report=report.id,
            )  # fmt: skip
            return "quiet"
        if held:
            log.info(f"deliver.when: {describe(held)}", step="when")

        drive_url = _drive_step(run, report, built, anchor)
        done = set()
        if uses_marker and not settings.force:
            done = delivered_targets(store, report.id, anchor)
        with run.step("deliver"):
            for delivery in report.spec.deliver.telegram:
                if delivery.to in done:
                    log.info(
                        f"the {delivery.to} chat got this report in an earlier run: skipped",
                        step="deliver", target=delivery.to,
                    )  # fmt: skip
                    continue
                _send(run, delivery, built, drive_url)

        if uses_marker:
            _write_marker(run, store, report, anchor, drive_url, settings.warehouse)
    return "sent"


def _write_index(run: Runner) -> None:
    with run.step("index", fatal=False, consequence="reports/index.json is not up to date."):
        write_index(data_store(run.settings), run.services.now())


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
    folder = render(
        spec.folder, context(report, anchor, report.params), f"{report.source}: deliver.drive"
    )
    if s.dry_run:
        run.log.info("dry run: no Drive upload", step="drive")
        run.deliveries.append(Delivery(channel="drive", status="dry_run", folder=folder))
        return None
    if not s.drive_folder_id or svc.drive is None:
        run.log.warning(
            "No Drive folder is configured (DRIVE_FOLDER_ID): the report goes out without a link",
            step="drive",
        )
        return None
    by_kind = {"pdf": built.pdf, "xlsx": built.xlsx, "json": built.bundle}
    files = [by_kind[kind] for kind in spec.files if by_kind[kind] is not None]
    url = None
    with run.step("drive", fatal=False, consequence="The report goes out without the Drive link."):
        url = svc.drive(s.drive_folder_id).upload_week(folder, files)
    status = "ok" if url else "failed"
    run.deliveries.append(Delivery(channel="drive", status=status, folder=folder, url=url))
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
        run.deliveries.append(Delivery(channel="telegram", target=target, status="dry_run"))
        return
    client, chat = run.services.telegram(), run.services.chat_id(target)
    results = []
    try:
        if photo is not None:
            results.append(client.send_photo(chat, photo, text))
            if document is not None:
                results.append(client.send_document(chat, document))
        elif document is not None:
            results.append(client.send_document(chat, document, text))
        else:
            results.append(client.send_message(chat, text))
    except Exception as error:
        run.deliveries.append(Delivery(
            channel="telegram", target=target, status="failed", message_ids=_ids(results),
            error=f"{type(error).__name__}: {scrub(str(error))}"[:300],
        ))  # fmt: skip
        raise
    run.deliveries.append(
        Delivery(channel="telegram", target=target, status="ok", message_ids=_ids(results))
    )
    run.log.info(f"sent {', '.join(sent) or 'a message'} to the {target} chat", step="deliver")


def _ids(results: list) -> list[int]:
    return [r["message_id"] for r in results if isinstance(r, dict) and "message_id" in r]


def _write_marker(
    run: Runner, store: Store, report: Report, anchor: date, drive_url: str | None, location: str
) -> None:
    key = marker_key(report.id, anchor)
    note = (
        f"The report {report.id} for {anchor} was sent, but the sent-marker could not be "
        "written. The ledger records the send; only an image from before the ledger, after a "
        "rollback, would send it again."
    )
    with run.step("marker", fatal=False, consequence=note):
        payload = {
            "report": report.id,
            "anchor": str(anchor),
            "sent_at": run.services.now().isoformat(timespec="seconds"),
            "to": sorted({d.to for d in report.spec.deliver.telegram}),
            "drive_url": drive_url,
            "warehouse": location,
        }
        store.write_text(key, json.dumps(payload, indent=2))


# --- every scheduled report -----------------------------------------------------------------------


def run_scheduled(
    reports: Sequence[Report], settings: RunSettings, services: Services, log: JsonLog | None = None
) -> int:
    """Run every report that has a schedule, one after the other on one extraction and
    warehouse. Returns 1 if any of them failed. The release's dry run uses this, so every
    scheduled report must build before a release is promoted."""
    log = log or JsonLog()
    scheduled = [r for r in sorted(reports, key=lambda r: r.id) if r.spec.schedule is not None]
    log.info(
        f"{len(scheduled)} scheduled report(s): {', '.join(r.id for r in scheduled) or 'none'}",
        step="start", dry_run=settings.dry_run,
    )  # fmt: skip
    if not scheduled:
        return 0
    run_id = settings.run_id or new_run_id(services.now())
    try:
        location = prepare(replace(settings, run_id=run_id), services, log, "scheduled reports")
    except StepFailed:
        log.error("scheduled reports failed", step="end")
        return 1
    shared = replace(settings, warehouse=location, run_id=run_id)
    failed = [r.id for r in scheduled if run_report_job(r, shared, services, log) != 0]
    if failed:
        log.error(f"scheduled report(s) failed: {', '.join(failed)}", step="end")
        return 1
    return 0
