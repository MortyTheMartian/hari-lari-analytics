"""`hari-lari-report run` (plan 9.7): one report or every scheduled one, from the extraction and
the warehouse build to the sent-marker, with fakes for Moka, Telegram and Drive. The report is
the tracer of `tests/fixtures/reports/`, on the fixture run (4 orders, 5 to 13 October 2026)."""

from __future__ import annotations

import io
import json
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from hari_lari_analytics.core.log import JsonLog
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import LocalStore
from hari_lari_analytics.distribution.drive import DriveUploader
from hari_lari_analytics.orchestration import report_cli
from hari_lari_analytics.orchestration.delivery import (
    RunSettings,
    Services,
    marker_keys,
    run_report_job,
    run_scheduled,
    token_expiry_notice,
)
from hari_lari_analytics.presentation.components import load_components
from hari_lari_analytics.presentation.reports import load_reports, parse_report
from hari_lari_analytics.sources.moka.run import RunResult

from .api_fixture import land_fixture
from .conftest import FIXTURES
from .fakes import ADMIN, GROUP, DriveSession, Telegram

ANCHOR = date(2026, 10, 13)
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=project().tz)
COMPONENTS = load_components()
TRACER = yaml.safe_load((FIXTURES / "reports" / "tracer.yml").read_text())


def report(**changes):
    spec = {**TRACER, **changes}
    return parse_report(spec, f"reports/{spec['id']}.yml", COMPONENTS)


class Env:
    """One test setup: settings, services and the things they record."""

    def __init__(self, tmp_path: Path, **settings):
        self.root = tmp_path / "data"
        self.telegram = Telegram(settings.pop("fail_on", None))
        self.drive_session = DriveSession(settings.pop("drive_fails", False))
        self.extract_calls = 0
        self.extract_error: Exception | None = None
        self.manifest = {"token_expires_at": "2027-05-11T00:00:00+08:00", "warnings": []}
        self.settings = RunSettings(
            store=LocalStore(self.root),
            config=settings.pop("config", str(FIXTURES)),
            anchor=settings.pop("anchor", ANCHOR),
            drive_folder_id=settings.pop("drive_folder_id", "root"),
            **settings,
        )
        self.services = Services(
            extract=self._extract,
            telegram=lambda: self.telegram.client,
            chat_id=lambda target: {"group": GROUP, "admin": ADMIN}[target],
            drive=lambda folder: DriveUploader(self.drive_session, folder),
            now=lambda: NOW,
        )
        self.out = io.StringIO()

    def _extract(self, out: str) -> RunResult:
        self.extract_calls += 1
        if self.extract_error:
            raise self.extract_error
        return RunResult(land_fixture(LocalStore(out)), self.manifest)

    def run(self, the_report=None) -> int:
        return run_report_job(the_report or report(), self.settings, self.services, self.log_())

    def log_(self) -> JsonLog:
        return JsonLog(self.out)

    def log(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# --- the happy path -------------------------------------------------------------------------------


def test_a_run_sends_the_photo_with_its_caption_then_the_pdf(env):
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]
    assert env.telegram.sent(ADMIN) == []
    caption = env.telegram.text(GROUP, "sendPhoto")
    assert "Tracer report, week 2026-W42" in caption
    assert "PDF and Excel lines: https://drive.google.com/drive/folders/id1" in caption
    assert "tracer_2026-10-13.pdf" in env.telegram.text(GROUP, "sendDocument")


def test_the_outputs_are_stored_per_report_and_anchor_and_the_marker_written(env):
    env.run()
    stored = sorted(p.name for p in (env.root / "reports" / "tracer" / "2026-10-13").iterdir())
    assert stored == [
        "bundle.json",
        "summary.txt",
        "tracer_2026-10-13.pdf",
        "tracer_2026-10-13.xlsx",
        "tracer_2026-10-13_overview.png",
    ]
    assert (env.root / "landing" / "moka" / "2026-10-14" / "tables").is_dir()
    marker = json.loads((env.root / "sent" / "tracer" / "2026-10-13.json").read_text())
    assert marker["report"] == "tracer" and marker["anchor"] == "2026-10-13"
    assert marker["to"] == ["group"] and marker["drive_url"].endswith("/id1")


def test_drive_gets_the_listed_files_in_the_folder_of_the_template(env):
    env.run()
    methods = [m for m, _ in env.drive_session.requests]
    assert methods == ["GET", "POST", "GET", "POST", "GET", "POST"]  # folder, pdf, xlsx


def test_every_step_is_logged_with_its_duration(env):
    env.run()
    done = {e["step"] for e in env.log() if "seconds" in e}
    steps = {"check", "extract", "warehouse", "model", "report", "upload", "drive", "deliver"}
    assert steps <= done
    assert env.log()[-1]["step"] == "end"


def test_an_existing_warehouse_is_not_extracted_or_built_again(env, warehouse):
    env.settings.warehouse = str(warehouse)
    assert env.run() == 0
    assert env.extract_calls == 0 and not (env.root / "warehouse").exists()
    marker = json.loads((env.root / "sent/tracer/2026-10-13.json").read_text())
    assert marker["warehouse"] == str(warehouse)


def test_the_warehouse_of_a_run_date_is_read_from_the_data_root(env, warehouse):
    shutil.copytree(warehouse.parent.parent.parent, env.root)
    env.settings.warehouse = "2026-10-14"
    assert env.run() == 0
    assert env.extract_calls == 0
    env.settings.warehouse, env.settings.force = "2026-10-20", True
    assert env.run() == 1
    assert "No warehouse of 2026-10-20" in env.telegram.text(ADMIN, "sendMessage")


def test_the_report_reads_the_warehouse_the_job_built(env):
    assert env.run() == 0
    built = json.loads((env.root / "warehouse" / "latest.json").read_text())
    assert built["date"] == "2026-10-14" and built["landing"]["moka"]["run_date"] == "2026-10-14"
    bundle = json.loads((env.root / "reports/tracer/2026-10-13/bundle.json").read_text())
    assert bundle["report"]["run"] == "2026-10-14"


# --- dry run, --to, the marker --------------------------------------------------------------------


def test_a_dry_run_sends_nothing_and_writes_under_dry_run(tmp_path):
    env = Env(tmp_path, dry_run=True)
    assert env.run() == 0
    assert env.telegram.calls == [] and env.drive_session.requests == []
    dry = env.root / "dry-run"
    assert len(list((dry / "reports" / "tracer" / "2026-10-13").iterdir())) == 5
    assert not (dry / "sent").exists() and not (env.root / "reports").exists()
    said = [e["message"] for e in env.log() if e["step"] == "deliver"]
    assert any("To group: tracer_2026-10-13_overview.png, tracer_2026-10-13.pdf" in m for m in said)
    assert not any("{{ drive_url }}" in m for m in said)


def test_a_dry_run_failure_messages_nobody(tmp_path):
    env = Env(tmp_path, dry_run=True)
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    assert env.telegram.calls == []


def test_a_second_run_says_already_sent(env):
    assert env.run() == 0
    calls = len(env.telegram.calls)
    env.extract_calls = 0
    assert env.run() == 0
    assert env.extract_calls == 0 and len(env.telegram.calls) == calls
    assert any(e.get("already_sent") for e in env.log())


def test_force_sends_again(env):
    env.run()
    env.settings.force = True
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"] * 2


def test_to_admin_sends_there_and_leaves_no_marker(tmp_path):
    env = Env(tmp_path, to="admin")
    assert env.run() == 0
    assert env.telegram.sent(ADMIN) == ["sendPhoto", "sendDocument"]
    assert env.telegram.sent(GROUP) == []
    assert not (env.root / "sent").exists()
    assert env.drive_session.requests  # a private check still uploads to Drive


def test_the_weekly_report_also_reads_the_marker_of_the_old_job(env):
    assert marker_keys("weekly", ANCHOR) == ["sent/weekly/2026-10-13.json", "sent/2026-10-13.json"]
    assert marker_keys("tracer", ANCHOR) == ["sent/tracer/2026-10-13.json"]
    env.settings.store.write_text("sent/2026-10-13.json", "{}")  # sent by hari-lari-weekly
    assert env.run(report(id="weekly")) == 0
    assert env.extract_calls == 0 and env.telegram.calls == []


# --- failures -------------------------------------------------------------------------------------


def test_a_failed_extraction_alerts_the_admin_naming_the_report(env):
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    alert = env.telegram.text(ADMIN, "sendMessage")
    assert "Hari Lari report tracer failed at step: extract" in alert and "Moka is down" in alert
    assert env.telegram.sent(GROUP) == [] and not (env.root / "sent").exists()
    assert [e["step"] for e in env.log() if e["severity"] == "ERROR"][0] == "extract"


def test_a_failed_send_reaches_the_admin_and_keeps_the_outputs(tmp_path):
    env = Env(tmp_path, fail_on="sendPhoto")
    assert env.run() == 1
    assert "step: deliver" in env.telegram.text(ADMIN, "sendMessage")
    assert not (env.root / "sent").exists()
    assert (env.root / "reports" / "tracer" / "2026-10-13").is_dir()


def test_a_failing_data_check_fails_the_warehouse_step(env, monkeypatch):
    from hari_lari_analytics.orchestration import delivery
    from hari_lari_analytics.warehouse.checks import CheckReport, DataCheckError

    def broken(*args, **kwargs):
        raise DataCheckError(CheckReport([]))

    monkeypatch.setattr(delivery, "build_warehouse", broken)
    assert env.run() == 1
    assert "step: warehouse" in env.telegram.text(ADMIN, "sendMessage")


def test_a_missing_config_file_fails_the_warehouse_step_with_the_admin_alert(tmp_path):
    env = Env(tmp_path, config=str(tmp_path / "no-config"))
    assert env.run() == 1
    alert = env.telegram.text(ADMIN, "sendMessage")
    assert "step: warehouse" in alert and "has no item_aliases.csv" in alert
    assert env.telegram.sent(GROUP) == []


def test_a_drive_failure_sends_the_text_without_the_link_line(tmp_path):
    env = Env(tmp_path, drive_fails=True)
    assert env.run() == 0
    caption = env.telegram.text(GROUP, "sendPhoto")
    assert "PDF and Excel lines" not in caption and "drive_url" not in caption
    assert "step drive failed" in env.telegram.text(ADMIN, "sendMessage")
    assert (env.root / "sent" / "tracer" / "2026-10-13.json").exists()


def test_a_marker_failure_warns_but_does_not_fail(env, monkeypatch):
    write_text = env.settings.store.write_text

    def broken(key, text):
        if key.startswith("sent/"):
            raise OSError("disk full")
        write_text(key, text)

    monkeypatch.setattr(env.settings.store, "write_text", broken)
    assert env.run() == 0  # a non-zero exit would make Cloud Run send the report twice
    assert "sent-marker could not be written" in env.telegram.text(ADMIN, "sendMessage")


def test_the_token_reminder_goes_to_the_admin_chat(env):
    env.manifest = {"token_expires_at": "2026-11-01T00:00:00+08:00", "warnings": []}
    assert env.run() == 0
    assert "Moka API token expires" in env.telegram.text(ADMIN, "sendMessage")


def test_reminder_when_fewer_than_45_days_remain():
    manifest = {"token_expires_at": "2026-11-20T10:00:00+08:00"}
    text = token_expiry_notice(manifest, NOW)
    assert "20 Nov 2026" in text and "37 days" in text


def test_no_reminder_with_45_days_or_more():
    manifest = {"token_expires_at": "2026-11-28T04:00:00+08:00"}
    assert token_expiry_notice(manifest, NOW) is None
    assert token_expiry_notice({}, NOW) is None


def test_an_expired_token_is_called_expired():
    manifest = {"token_expires_at": "2026-10-01T00:00:00+08:00"}
    assert "expired on 1 Oct 2026" in token_expiry_notice(manifest, NOW)


# --- deliver.when ---------------------------------------------------------------------------------


def _with_when(*conditions):
    return report(deliver={**TRACER["deliver"], "when": list(conditions)})


def test_without_a_condition_that_holds_nothing_is_sent_and_the_run_succeeds(env):
    the_report = _with_when({"insight": "discount_rate_high"})
    assert env.run(the_report) == 0
    assert env.telegram.calls == [] and env.drive_session.requests == []
    assert not (env.root / "sent").exists()
    assert (env.root / "reports" / "tracer" / "2026-10-13").is_dir()  # built and stored
    assert any("insight discount_rate_high: no" in e["message"] for e in env.log())


def test_any_one_condition_that_holds_sends(env):
    the_report = _with_when(
        {"insight": "discount_rate_high"}, {"rows": "category_contributive_aov"}
    )
    assert env.run(the_report) == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]
    assert any("rows category_contributive_aov: yes" in e["message"] for e in env.log())


# --- every scheduled report -----------------------------------------------------------------------


def test_scheduled_runs_only_the_reports_with_a_schedule_on_one_extraction(env):
    scheduled = report(id="weekly", schedule={"cron": "0 4 * * 1"})
    other = report(id="other", schedule={"cron": "0 6 * * *", "enabled": False})
    on_demand = report(id="tracer")
    env.settings.dry_run = True
    reports = [scheduled, other, on_demand]
    assert run_scheduled(reports, env.settings, env.services, env.log_()) == 0
    assert env.extract_calls == 1
    built = sorted(p.name for p in (env.root / "dry-run" / "reports").iterdir())
    assert built == ["other", "weekly"]


def test_scheduled_fails_when_one_report_fails(env, monkeypatch):
    from hari_lari_analytics.orchestration import delivery

    real = delivery.build_report

    def fails_for_b(the_report, *args, **kwargs):
        if the_report.id == "b":
            raise RuntimeError("broken")
        return real(the_report, *args, **kwargs)

    monkeypatch.setattr(delivery, "build_report", fails_for_b)
    env.settings.dry_run = True
    reports = [report(id=i, schedule={"cron": "0 4 * * 1"}) for i in ("a", "b")]
    assert run_scheduled(reports, env.settings, env.services, env.log_()) == 1
    assert (env.root / "dry-run" / "reports" / "a").is_dir()


# --- the command ----------------------------------------------------------------------------------


def test_the_packaged_weekly_report_is_scheduled_on_monday_at_four():
    reports = load_reports(COMPONENTS)
    (row,) = report_cli.schedules(reports)
    assert row == {
        "report": "weekly",
        "cron": "0 4 * * 1",
        "timezone": "Asia/Makassar",
        "enabled": True,
        "args": ["run", "--report", "weekly"],
    }
    drive = reports["weekly"].spec.deliver.drive
    assert drive.folder == "{{ anchor }} (week {{ anchor_week }})"  # the existing week folders


def test_the_run_command_dry_runs_a_report_on_an_existing_warehouse(warehouse, tmp_path, capsys):
    code = report_cli.main([
        "--reports", str(FIXTURES / "reports"),
        "run", "--report", "tracer", "--dry-run", "--warehouse", str(warehouse),
        "--anchor", "2026-10-13", "--data-root", str(tmp_path),
    ])  # fmt: skip
    assert code == 0
    assert (tmp_path / "dry-run" / "reports" / "tracer" / "2026-10-13" / "bundle.json").exists()
    assert "dry run: nothing is sent" in capsys.readouterr().out


def test_run_needs_a_report_or_scheduled():
    with pytest.raises(SystemExit):
        report_cli.main(["run", "--dry-run"])
