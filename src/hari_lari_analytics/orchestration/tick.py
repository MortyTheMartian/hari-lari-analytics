"""The daily tick (D22, D27 to D30; docs/architecture.md, "A day"): `hari-lari-pipeline tick`.

One run extracts each source that has not landed on the run date, builds the warehouse when a
landing run, the config or the image changed, and runs every report that is due on the due
date, each on its own: one failing report does not stop the next. Every step writes its ledger
record, so a second tick on the same date (the 06:00 retry) does only what the first left
undone.

A report is due when the day fields of its cron match the due date (`cron_matches`) and it is
enabled; it runs only when every source it needs landed on the run date, else it is skipped.
After the last tick of the day (project.yml `tick.times`), the scheduled tick tells the chats
of a due report that is still not sent that it is late, and gives the admin the command that
sends it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from hari_lari_analytics.core.landing import LANDING, latest_landed, new_run_id
from hari_lari_analytics.core.log import JsonLog
from hari_lari_analytics.core.project import project
from hari_lari_analytics.distribution.telegram import scrub, send_notice
from hari_lari_analytics.orchestration.delivery import (
    Runner,
    RunSettings,
    Services,
    StepFailed,
    already_sent,
    data_store,
    extract_step,
    run_report_job,
    warehouse_step,
)
from hari_lari_analytics.orchestration.ledger import Delivery, report_records, write_record
from hari_lari_analytics.presentation.reports import Report, cron_matches, resolve_anchor
from hari_lari_analytics.warehouse.build import up_to_date
from hari_lari_analytics.warehouse.db import source_names
from hari_lari_analytics.warehouse.inputs import InputError


@dataclass
class TickOptions:
    day: date | None = None  # the due date (default: the run date)
    only: str | None = None  # one report instead of every due one
    all_scheduled: bool = False  # every report with a schedule counts as due (the release check)


def due_reports(
    reports: Sequence[Report], day: date, only: str | None = None, all_scheduled: bool = False
) -> list[Report]:
    """The reports the tick runs for the due date `day`, by id."""
    scheduled = sorted((r for r in reports if r.spec.schedule is not None), key=lambda r: r.id)
    if only is not None:
        if only not in {r.id for r in scheduled}:
            known = ", ".join(r.id for r in scheduled) or "none"
            raise InputError(f"{only} is not a report with a schedule (known: {known})")
        scheduled = [r for r in scheduled if r.id == only]
    if all_scheduled:
        return scheduled
    return [
        r
        for r in scheduled
        if r.spec.schedule is not None
        and r.spec.schedule.enabled
        and cron_matches(r.spec.schedule.cron, day)
    ]


def report_sources(report: Report) -> list[str]:
    """The sources a report needs. Every report needs every source while `moka` is the only
    one; D29's derivation from the models a report reads replaces this with a second source."""
    return source_names()


def run_tick(
    reports: Sequence[Report],
    settings: RunSettings,
    services: Services,
    log: JsonLog | None = None,
    options: TickOptions | None = None,
) -> int:
    """One tick. Returns 1 if a source, the warehouse or a report failed, else 0."""
    log = log or JsonLog()
    options = options or TickOptions()
    p = project()
    now = services.now().astimezone(p.tz)
    run_date = p.today(now)
    day = options.day or run_date
    settings = replace(settings, run_id=settings.run_id or new_run_id(now))
    last = p.tick.is_last(now)
    note = _retry_note(settings, now, last)
    due = due_reports(reports, day, options.only, options.all_scheduled)
    log.info(
        f"tick {settings.run_id}: run date {run_date}, due on {day}: "
        f"{', '.join(r.id for r in due) or 'nothing'}",
        step="tick", run_id=settings.run_id, due=[r.id for r in due], dry_run=settings.dry_run,
        last_tick=last,
    )  # fmt: skip
    store = data_store(settings)
    failed = False

    missing: dict[str, str] = {}
    for source in source_names():
        if latest_landed(store.child(LANDING), source, run_date) == run_date:
            log.info(f"{source} already landed on {run_date}", step="extract", source=source)
            continue
        try:
            extract_step(settings, services, log, f"tick, source {source}", source, note)
        except StepFailed:
            failed = True
            missing[source] = f"{source} did not land on {run_date}"

    location = None
    try:
        info = up_to_date(store, run_date, settings.config)
    except Exception as error:  # noqa: BLE001 - the build below reports it properly
        log.warning(f"could not compare the warehouse: {scrub(str(error))}", step="warehouse")
        info = None
    if info is not None:
        log.info(f"the warehouse of {run_date} is up to date", step="warehouse")
    else:
        try:
            info = warehouse_step(settings, services, log, "tick, warehouse", note)
        except StepFailed:
            failed = True
    if info is not None:
        location = store.url(info.database)

    anchors = {}
    for report in due:
        anchor = resolve_anchor(report, None, datetime.combine(day, now.timetz()))
        anchors[report.id] = anchor
        job = replace(settings, anchor=anchor, warehouse=location)
        lost = [missing[s] for s in report_sources(report) if s in missing]
        if location is None or lost:
            reason = "; ".join(lost) if lost else "the warehouse could not be built"
            failed |= _skip(report, job, services, log, anchor, reason)
            continue
        try:
            failed |= run_report_job(report, job, services, log) != 0
        except Exception as error:  # noqa: BLE001 - one report must not stop the next
            log.error(f"report {report.id} crashed: {scrub(str(error))}", step="report")
            failed = True

    if settings.trigger == "scheduler" and last and not settings.dry_run and settings.to is None:
        for report in due:
            _late_notice(report, anchors[report.id], day, settings, services, log)
    log.info(f"tick {settings.run_id} finished", step="end", failed=failed)
    return 1 if failed else 0


def _retry_note(settings: RunSettings, now: datetime, last: bool) -> str:
    if settings.trigger != "scheduler":
        return "A run by hand: run the tick again to retry."
    if last:
        return "This was the last tick today."
    later = [t for t in project().tick.times if t > f"{now:%H:%M}"]
    return f"The {later[0]} tick tries again." if later else "This was the last tick today."


def _skip(
    report: Report,
    settings: RunSettings,
    services: Services,
    log: JsonLog,
    anchor: date,
    reason: str,
) -> bool:
    """Record a due report as skipped; returns whether that is a failure (False when it was
    already sent)."""
    real = not settings.dry_run and settings.to is None and not settings.force
    if real and already_sent(data_store(settings), report.id, anchor):
        log.info(f"report {report.id} for {anchor} was already sent", step="check")
        return False
    run = Runner(settings, services, log, f"report {report.id}")
    run.record(
        f"report-{report.id}", "skipped", reason=reason,
        inputs={"report": report.id, "anchor": f"{anchor:%Y-%m-%d}"},
    )  # fmt: skip
    log.warning(f"report {report.id} skipped: {reason}", step="report", report=report.id)
    return True


def _late_notice(
    report: Report,
    anchor: date,
    day: date,
    settings: RunSettings,
    services: Services,
    log: JsonLog,
) -> None:
    """Tell the chats of a due report that is still not sent that it is late (D30), and the
    admin how to send it. The messages are deliveries of the report's last record."""
    store = data_store(settings)
    if already_sent(store, report.id, anchor):
        return
    records = [r for r in report_records(store, report.id, anchor) if not r.dry_run and not r.to]
    latest = records[-1] if records else None
    if latest is not None and latest.status == "quiet":
        return  # deliver.when held no condition: nothing was due to go out
    if any(d.kind == "late_notice" for r in records for d in r.deliveries):
        return  # told already (Cloud Run retries a tick that failed)
    text = (
        f"📭 {report.spec.title} up to {anchor:%a %-d %b %Y} could not be sent this morning. "
        "It follows later today."
    )
    deliveries = list(latest.deliveries) if latest else []
    for target in dict.fromkeys(d.to for d in report.spec.deliver.telegram):
        try:
            sent = services.telegram().send_message(services.chat_id(target), text)
            ids = [sent["message_id"]] if isinstance(sent, dict) and "message_id" in sent else []
            deliveries.append(Delivery(channel="telegram", kind="late_notice", target=target,
                                       status="ok", message_ids=ids))  # fmt: skip
        except Exception as error:  # noqa: BLE001
            message = f"{type(error).__name__}: {scrub(str(error))}"[:900]
            deliveries.append(Delivery(channel="telegram", kind="late_notice", target=target,
                                       status="failed", error=message))  # fmt: skip
    why = (latest.reason or latest.error or latest.status) if latest else "no record"
    send_notice(
        services.telegram(), services.chat_id("admin"),
        f"⚠️ Hari Lari report {report.id} for {anchor} was not sent by the last tick ({why}). "
        f"Its chats were told it is late. Send it with:\n"
        f"hari-lari-pipeline tick --date {day} --only {report.id}",
    )  # fmt: skip
    log.warning(f"report {report.id} for {anchor} is late; its chats were told", step="late")
    if latest is not None:
        try:
            write_record(store, latest.model_copy(update={"deliveries": deliveries}))
        except Exception as error:  # noqa: BLE001
            log.warning(f"could not record the late notice: {scrub(str(error))}", step="late")
