"""`hari-lari-pipeline tick` (plan t16, D27 to D30): the due rules, the tick times, and the
failure policy, with the fakes of test_report_run (a fake Moka source that can fail)."""

from __future__ import annotations

import io
import json
from datetime import date, datetime
from importlib import resources

import pytest
import yaml

from hari_lari_analytics.core.project import ProjectError, parse_project, project
from hari_lari_analytics.core.store import LocalStore
from hari_lari_analytics.orchestration import delivery, pipeline_cli
from hari_lari_analytics.orchestration.ledger import delivered_targets, read_records
from hari_lari_analytics.orchestration.tick import TickOptions, due_reports, run_tick
from hari_lari_analytics.presentation.reports import ReportError, cron_matches
from hari_lari_analytics.warehouse.inputs import InputError

from .api_fixture import land_fixture
from .fakes import ADMIN, GROUP
from .test_report_run import NOW, Env, report

WEDNESDAY = date(2026, 10, 14)  # NOW's date: the run date of the fixture's landing run
AT_SIX = NOW.replace(hour=6)


# --- when a report is due -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cron", "day", "due"),
    [
        ("0 4 * * 1", date(2026, 11, 30), True),  # Monday
        ("0 4 * * 1", date(2026, 12, 1), False),
        ("0 4 * * mon", date(2026, 11, 30), True),
        ("0 4 * * 0", date(2026, 11, 29), True),  # Sunday is 0 ...
        ("0 4 * * 7", date(2026, 11, 29), True),  # ... and 7
        ("0 4 * * 1-5", date(2026, 11, 28), False),  # Saturday
        ("0 4 1 * *", date(2026, 12, 1), True),
        ("0 4 1 * *", date(2026, 11, 30), False),
        ("0 4 1 * 1", date(2026, 11, 30), True),  # both day fields set: either matches
        ("0 4 1 * 1", date(2026, 12, 1), True),
        ("0 4 1 * 1", date(2026, 12, 2), False),
        ("0 4 */2 * 1", date(2026, 11, 30), False),  # `*/2` counts as unrestricted: both
        ("0 4 */2 * 1", date(2026, 12, 7), True),
        ("0 4 * dec mon", date(2026, 11, 30), False),
        ("0 4 * dec mon", date(2026, 12, 7), True),
        ("0 4 * * *", date(2026, 2, 28), True),
    ],
)
def test_the_day_fields_of_the_cron_decide_the_due_dates(cron, day, due):
    assert cron_matches(cron, day) is due


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"cron": "0 */6 * * *"}, "the minute and the hour must each be one number"),
        ({"cron": "0,30 4 * * 1"}, "the minute and the hour must each be one number"),
        ({"cron": "0 4-6 * * 1"}, "the minute and the hour must each be one number"),
        ({"cron": "0 4 * * 1", "timezone": "Asia/Makassar"}, "timezone: remove the key"),
    ],
)
def test_the_loader_refuses_times_the_tick_cannot_keep_and_the_timezone_key(schedule, message):
    with pytest.raises(ReportError, match=message):
        report(schedule=schedule)


def test_due_reports_follow_the_day_and_enabled_and_only():
    weekly = report(id="weekly", schedule={"cron": "0 4 * * 1"})
    daily = report(id="daily", schedule={"cron": "0 4 * * *"})
    paused = report(id="paused", schedule={"cron": "0 4 * * *", "enabled": False})
    on_demand = report(id="tracer")
    every = [weekly, daily, paused, on_demand]
    ids = lambda found: [r.id for r in found]  # noqa: E731
    assert ids(due_reports(every, date(2026, 11, 30))) == ["daily", "weekly"]
    assert ids(due_reports(every, date(2026, 12, 1))) == ["daily"]
    assert ids(due_reports(every, date(2026, 12, 1), only="weekly")) == []
    assert ids(due_reports(every, date(2026, 12, 1), all_scheduled=True)) == [
        "daily", "paused", "weekly",
    ]  # fmt: skip
    with pytest.raises(InputError, match="tracer is not a report with a schedule"):
        due_reports(every, date(2026, 12, 1), only="tracer")


def test_the_packaged_weekly_report_is_due_on_mondays_only():
    from hari_lari_analytics.presentation.components import load_components
    from hari_lari_analytics.presentation.reports import load_reports

    reports = list(load_reports(load_components()).values())
    assert [r.id for r in due_reports(reports, date(2026, 11, 30))] == ["weekly"]
    assert due_reports(reports, date(2026, 12, 1)) == []


# --- the tick times -------------------------------------------------------------------------------


def _project(**tick) -> object:
    packaged = resources.files("hari_lari_analytics.core") / "project.yml"
    raw = yaml.safe_load(packaged.read_text())
    return parse_project(yaml.safe_dump({**raw, "tick": tick}))


def test_the_tick_times_give_the_cron_and_the_last_tick():
    tick = project().tick
    assert tick.times == ["04:00", "06:00"] and tick.cron == "0 4,6 * * *"
    assert not tick.is_last(datetime(2026, 11, 30, 4, 0, 40))
    assert tick.is_last(datetime(2026, 11, 30, 5, 50))
    assert tick.is_last(datetime(2026, 11, 30, 6, 1, tzinfo=project().tz))


@pytest.mark.parametrize(
    ("times", "message"),
    [
        (["4:00"], "a time is HH:MM"),
        (["06:00", "04:00"], "earliest first"),
        (["04:00", "06:30"], "share their minute"),
        ([], "at least 1"),
    ],
)
def test_bad_tick_times_are_refused(times, message):
    with pytest.raises(ProjectError, match=message):
        _project(times=times)


def test_the_schedule_command_prints_the_one_scheduler_job():
    out = io.StringIO()
    assert pipeline_cli.main(["schedule", "--json"], out) == 0
    assert json.loads(out.getvalue()) == {
        "job": "hari-lari-tick", "cron": "0 4,6 * * *", "timezone": "Asia/Makassar",
        "args": ["tick", "--scheduler"],
    }  # fmt: skip


def test_other_commands_go_to_hari_lari_report(capsys):
    assert pipeline_cli.main(["schedules"]) == 0
    assert "weekly: 0 4 * * 1 Asia/Makassar" in capsys.readouterr().out


# --- a tick ---------------------------------------------------------------------------------------

DUE = {"cron": "0 4 * * 3"}  # Wednesdays: due on NOW
NOT_DUE = {"cron": "0 4 * * 1"}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, anchor=None)


def tick(env: Env, reports, *, at=None, trigger="manual", **options) -> int:
    if at is not None:
        env.services.now = lambda: at
    env.settings.trigger = trigger
    return run_tick(reports, env.settings, env.services, env.log_(), TickOptions(**options))


def steps(env: Env) -> list[tuple[str, str]]:
    return [(r.step, r.status) for r in read_records(LocalStore(env.root))]


def test_a_tick_extracts_builds_and_runs_only_the_due_reports(env):
    assert tick(env, [report(id="a", schedule=DUE), report(id="b", schedule=NOT_DUE)]) == 0
    assert env.extract_calls == 1
    assert steps(env) == [("extract-moka", "ok"), ("warehouse", "ok"), ("report-a", "sent")]
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]
    assert (env.root / "reports" / "a" / "2026-10-13").is_dir()  # the anchor: the day before
    assert {r.run_id for r in read_records(LocalStore(env.root))} == {env.log()[0]["run_id"]}


def test_the_retry_tick_does_only_what_is_left(env):
    reports = [report(id="a", schedule=DUE)]
    assert tick(env, reports) == 0
    sent = len(env.telegram.calls)
    assert tick(env, reports, at=AT_SIX, trigger="scheduler") == 0
    assert env.extract_calls == 1  # moka already landed today
    assert len(env.telegram.calls) == sent  # already sent, and no late notice
    assert len(steps(env)) == 3  # the warehouse is up to date: no new record
    assert any("is up to date" in e["message"] for e in env.log())


def test_the_date_changes_which_reports_are_due_and_their_anchor(env):
    env.settings.dry_run = True
    reports = [report(id="a", schedule=DUE), report(id="b", schedule=NOT_DUE)]
    assert tick(env, reports, day=date(2026, 10, 12)) == 0  # a Monday
    built = env.root / "dry-run" / "reports"
    assert sorted(p.name for p in built.iterdir() if p.is_dir()) == ["b"]
    assert (built / "b" / "2026-10-11").is_dir()
    assert env.telegram.calls == []


# --- the failure policy ---------------------------------------------------------------------------


def failing_source(env: Env) -> None:
    """Moka fails today; yesterday's run landed, so the warehouse still builds."""
    land_fixture(LocalStore(env.root / "landing"), date(2026, 10, 13),
                 started_at=datetime(2026, 10, 13, 4, 0))  # fmt: skip
    env.extract_error = RuntimeError("Moka API: 503 Service Unavailable")


def test_a_failing_source_skips_the_reports_that_need_it_and_the_warehouse_still_builds(env):
    failing_source(env)
    assert tick(env, [report(id="a", schedule=DUE)], trigger="scheduler") == 1
    assert steps(env) == [("extract-moka", "failed"), ("warehouse", "ok"), ("report-a", "skipped")]
    (skipped,) = [r for r in read_records(LocalStore(env.root)) if r.step == "report-a"]
    assert skipped.reason == "moka did not land on 2026-10-14"
    assert env.telegram.sent(GROUP) == []
    (alert,) = [b.decode() for m, c, b in env.telegram.calls if c == ADMIN]
    assert "failed at step: extract" in alert and "The 06:00 tick tries again." in alert


def test_after_the_last_tick_a_report_not_sent_is_announced_to_its_chats(env):
    failing_source(env)
    reports = [report(id="a", schedule=DUE)]
    tick(env, reports, trigger="scheduler")
    assert tick(env, reports, at=AT_SIX, trigger="scheduler") == 1
    group = env.telegram.text(GROUP, "sendMessage")
    assert "Tracer report up to Tue 13 Oct 2026 could not be sent this morning" in group
    assert "503" not in group  # the chats get no error
    admin = [b.decode() for m, c, b in env.telegram.calls if c == ADMIN]
    assert "This was the last tick today." in admin[-2]
    assert "hari-lari-pipeline tick --date 2026-10-14 --only a" in admin[-1]
    last = read_records(LocalStore(env.root), step="report-a")[-1]
    (notice,) = last.deliveries
    assert (notice.kind, notice.target, notice.status) == ("late_notice", "group", "ok")
    assert notice.message_ids
    # the notice is not the report: a send by hand still goes to the group
    assert delivered_targets(LocalStore(env.root), "a", date(2026, 10, 13)) == set()
    # Cloud Run retries the failed tick once: the chats are not told twice
    tick(env, reports, at=AT_SIX.replace(minute=2), trigger="scheduler")
    assert env.telegram.sent(GROUP) == ["sendMessage"]


@pytest.mark.parametrize("change", ["manual", "dry_run", "to"])
def test_only_the_scheduled_tick_announces_a_late_report(env, change):
    failing_source(env)
    if change == "dry_run":
        env.settings.dry_run = True
        land_fixture(LocalStore(env.root / "dry-run" / "landing"), date(2026, 10, 13),
                     started_at=datetime(2026, 10, 13, 4, 0))  # fmt: skip
    if change == "to":
        env.settings.to = "admin"
    trigger = "manual" if change == "manual" else "scheduler"
    assert tick(env, [report(id="a", schedule=DUE)], at=AT_SIX, trigger=trigger) == 1
    assert env.telegram.sent(GROUP) == []
    assert not any(b"could not be sent this morning" in b for _, _, b in env.telegram.calls)


def test_one_failing_report_does_not_stop_the_others(env, monkeypatch):
    real = delivery.build_report

    def fails_for_b(the_report, *args, **kwargs):
        if the_report.id == "b":
            raise RuntimeError("broken")
        return real(the_report, *args, **kwargs)

    monkeypatch.setattr(delivery, "build_report", fails_for_b)
    reports = [report(id=i, schedule=DUE) for i in ("a", "b", "c")]
    assert tick(env, reports) == 1
    reported = [s for s in steps(env) if s[0].startswith("report-")]
    assert reported == [("report-a", "sent"), ("report-b", "failed"), ("report-c", "sent")]
    assert env.telegram.sent(GROUP).count("sendPhoto") == 2


def test_a_failing_warehouse_skips_every_due_report(env, tmp_path):
    env.settings.config = str(tmp_path / "no-config")
    assert tick(env, [report(id="a", schedule=DUE)]) == 1
    assert steps(env) == [("extract-moka", "ok"), ("warehouse", "failed"), ("report-a", "skipped")]
    skipped = read_records(LocalStore(env.root), step="report-a")[-1]
    assert skipped.reason == "the warehouse could not be built"
