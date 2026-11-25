import io
import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from hari_lari_analytics.deliver.drive import DriveError, DriveUploader
from hari_lari_analytics.deliver.telegram import TelegramClient
from hari_lari_analytics.extract.run import RunResult
from hari_lari_analytics.project import project
from hari_lari_analytics.weekly.log import JsonLog
from hari_lari_analytics.weekly.run import (
    Services,
    Settings,
    run_weekly,
    token_expiry_notice,
    week_folder_name,
)
from hari_lari_analytics.weekly.store import GcsStore, LocalStore

from .api_fixture import api_tables
from .conftest import FIXTURES

TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake_12"
GROUP, ADMIN = "-100111", "999"
ANCHOR = date(2026, 10, 13)
TZ = project().tz
NOW = datetime(2026, 10, 14, 4, 0, tzinfo=TZ)


# ---- fakes -----------------------------------------------------------------------
class Telegram:
    """A TelegramClient on a recording transport."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, str, bytes]] = []  # method, chat id, body
        self.fail_on = fail_on
        self.client = TelegramClient(TOKEN, transport=self._transport, sleep=lambda s: None)

    def _transport(self, url, body, headers, timeout):
        method = url.rsplit("/", 1)[-1]
        chat = re.search(rb'name="chat_id"\r\n\r\n(-?\d+)', body).group(1).decode()
        self.calls.append((method, chat, body))
        if method == self.fail_on:
            return 400, b'{"ok": false, "description": "Bad Request: chat not found"}'
        return 200, b'{"ok": true, "result": {}}'

    def sent(self, chat: str) -> list[str]:
        return [m for m, c, _ in self.calls if c == chat]

    def text(self, chat: str, method: str) -> str:
        return next(b for m, c, b in self.calls if c == chat and m == method).decode("latin-1")


class DriveSession:
    def __init__(self, fail: bool = False):
        self.requests: list[tuple[str, str]] = []
        self.fail = fail
        self.ids = iter(range(1, 100))

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        if self.fail:
            return _Response(
                403, {"error": {"message": "Service Accounts do not have storage quota"}}
            )
        if method == "GET":
            return _Response(200, {"files": []})
        return _Response(200, {"id": f"id{next(self.ids)}"})


class _Response:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class Env:
    """One test setup: settings, services and the things they record."""

    def __init__(self, tmp_path: Path, **settings):
        self.root = tmp_path / "data"
        self.telegram = Telegram(settings.pop("fail_on", None))
        self.drive_session = DriveSession(settings.pop("drive_fails", False))
        self.extract_calls = 0
        self.extract_error: Exception | None = None
        self.manifest = {"token_expires_at": "2027-05-11T00:00:00+08:00", "warnings": []}
        self.settings = Settings(
            store=LocalStore(self.root),
            aliases=FIXTURES / "item_aliases.csv",
            category_groups=FIXTURES.parent.parent / "config" / "category_group.csv",
            anchor=ANCHOR,
            drive_folder_id=settings.pop("drive_folder_id", "root"),
            **settings,
        )
        self.services = Services(
            extract=self._extract,
            telegram=lambda: self.telegram.client,
            chat_id=lambda target: GROUP if target == "group" else ADMIN,
            drive=lambda folder: DriveUploader(self.drive_session, folder),
            now=lambda: NOW,
        )
        self.out = io.StringIO()

    def _extract(self, out: str) -> RunResult:
        self.extract_calls += 1
        if self.extract_error:
            raise self.extract_error
        run = Path(out) / "2026-10-14"
        (run / "tables").mkdir(parents=True, exist_ok=True)
        for name, table in api_tables().items():
            table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
        return RunResult(str(run), self.manifest)

    def run(self) -> int:
        return run_weekly(self.settings, self.services, JsonLog(self.out))

    def log(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---- 5.1 the happy path -----------------------------------------------------------
def test_full_run_sends_to_the_group_and_stores_everything(env):
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]
    assert env.telegram.sent(ADMIN) == []
    reports = env.root / "reports" / "2026-10-13"
    assert sorted(p.suffix for p in reports.iterdir()) == [".pdf", ".png", ".xlsx"]
    assert (env.root / "raw" / "2026-10-14" / "tables").is_dir()
    marker = json.loads((env.root / "sent" / "2026-10-13.json").read_text())
    assert marker["anchor"] == "2026-10-13" and marker["to"] == "group"


def test_the_caption_has_the_drive_link(env):
    env.run()
    caption = env.telegram.text(GROUP, "sendPhoto")
    assert "Hari Lari weekly sales report" in caption
    assert "PDF and Excel lines: https://drive.google.com/drive/folders/id1" in caption
    methods = [m for m, _ in env.drive_session.requests]
    assert methods == ["GET", "POST", "GET", "POST", "GET", "POST"]  # folder, pdf, xlsx


def test_every_step_is_logged_with_its_duration(env):
    env.run()
    done = {e["step"]: e for e in env.log() if e["message"].endswith("s") and "seconds" in e}
    for step in ["check", "extract", "model", "report", "upload", "drive", "deliver"]:
        assert step in done and done[step]["seconds"] >= 0
    assert set(env.log()[-1]["seconds_by_step"]) >= {"extract", "model", "report", "deliver"}


def test_the_anchor_defaults_to_yesterday_in_bali(env):
    env.settings.anchor = None  # NOW is 14 Oct 04:00, so yesterday is 13 Oct
    assert env.run() == 0
    assert (env.root / "sent" / "2026-10-13.json").exists()


# ---- 5.2 dry run ------------------------------------------------------------------
def test_dry_run_sends_nothing_and_writes_under_dry_run(tmp_path):
    env = Env(tmp_path, dry_run=True)
    assert env.run() == 0
    assert env.telegram.calls == []
    assert env.drive_session.requests == []
    assert not (env.root / "sent").exists()
    assert not (env.root / "reports").exists() and not (env.root / "raw").exists()
    assert len(list((env.root / "dry-run" / "reports" / "2026-10-13").iterdir())) == 3
    assert any("The caption would be" in e["message"] for e in env.log())


def test_dry_run_ignores_the_marker_and_makes_none(tmp_path):
    env = Env(tmp_path)
    env.settings.store.write_text("sent/2026-10-13.json", "{}")
    env.settings.dry_run = True
    assert env.run() == 0
    assert env.extract_calls == 1


def test_dry_run_failure_does_not_message_anyone(tmp_path):
    env = Env(tmp_path, dry_run=True)
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    assert env.telegram.calls == []


# ---- 5.3 idempotent send ----------------------------------------------------------
def test_second_run_says_already_sent_and_does_nothing(env):
    assert env.run() == 0
    calls = len(env.telegram.calls)
    env.extract_calls = 0
    env.out = io.StringIO()
    assert env.run() == 0
    assert env.extract_calls == 0 and len(env.telegram.calls) == calls
    assert any(e.get("already_sent") for e in env.log())


def test_force_sends_again(env):
    env.run()
    env.settings.force = True
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"] * 2


def test_the_marker_is_per_anchor(env):
    env.run()
    env.settings.store.write_text("sent/2026-10-06.json", "{}")  # an older week
    assert (env.root / "sent" / "2026-10-13.json").exists()
    env.settings.anchor = date(2026, 10, 5)
    assert env.run() == 0  # a different anchor is not blocked
    assert env.extract_calls == 2


def test_admin_run_goes_to_the_admin_chat_and_leaves_no_marker(tmp_path):
    env = Env(tmp_path, to="admin")
    assert env.run() == 0
    assert env.telegram.sent(ADMIN) == ["sendPhoto", "sendDocument"]
    assert env.telegram.sent(GROUP) == []
    assert not (env.root / "sent").exists()
    env.settings.to = "group"  # the real send is not blocked by the private one
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]


# ---- 5.4 failures -----------------------------------------------------------------
def test_a_failed_extraction_alerts_the_admin_and_fails_the_run(env):
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    assert env.telegram.sent(GROUP) == []
    assert env.telegram.sent(ADMIN) == ["sendMessage"]
    alert = env.telegram.text(ADMIN, "sendMessage")
    assert "failed at step: extract" in alert and "Moka is down" in alert
    assert not (env.root / "sent").exists()
    errors = [e for e in env.log() if e["severity"] == "ERROR"]
    assert errors and errors[0]["step"] == "extract"


def test_a_failed_send_reaches_the_admin(tmp_path):
    env = Env(tmp_path, fail_on="sendPhoto")
    assert env.run() == 1
    assert env.telegram.sent(GROUP) == ["sendPhoto"]
    assert env.telegram.sent(ADMIN) == ["sendMessage"]
    assert "step: deliver" in env.telegram.text(ADMIN, "sendMessage")
    assert not (env.root / "sent").exists()
    assert (env.root / "reports" / "2026-10-13").is_dir()  # kept for a manual re-send


def test_a_sales_free_anchor_fails_the_model_step(env):
    env.settings.anchor = date(2026, 1, 1)
    assert env.run() == 1
    assert "step: model" in env.telegram.text(ADMIN, "sendMessage")


def test_the_token_is_not_in_the_alert_or_the_log(env):
    env.extract_error = RuntimeError(f"url had {TOKEN}")
    env.run()
    assert TOKEN not in env.telegram.text(ADMIN, "sendMessage")
    assert TOKEN not in env.out.getvalue()


def test_alert_failure_does_not_hide_the_error(tmp_path):
    env = Env(tmp_path)
    env.services.telegram = lambda: (_ for _ in ()).throw(RuntimeError("no secret"))
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    messages = [e["message"] for e in env.log()]
    assert any("extract failed: Moka is down" in m for m in messages)
    assert any("could not send the admin alert" in m for m in messages)


# ---- the Drive step and the marker never fail the run -----------------------------
def test_a_drive_failure_sends_the_report_without_a_link(tmp_path):
    env = Env(tmp_path, drive_fails=True)
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]
    assert "drive.google.com" not in env.telegram.text(GROUP, "sendPhoto")
    notice = env.telegram.text(ADMIN, "sendMessage")
    assert "step drive failed" in notice and "without the Drive link" in notice
    assert (env.root / "sent" / "2026-10-13.json").exists()
    assert not any(e["severity"] == "ERROR" for e in env.log())


def test_without_a_drive_folder_the_report_goes_out_without_a_link(tmp_path):
    env = Env(tmp_path, drive_folder_id=None)
    assert env.run() == 0
    assert env.drive_session.requests == []
    assert "drive.google.com" not in env.telegram.text(GROUP, "sendPhoto")
    assert any("No Drive folder" in e["message"] for e in env.log())


def test_a_marker_failure_warns_but_does_not_fail(env, monkeypatch):
    def broken(key, text):
        raise OSError("disk full")

    monkeypatch.setattr(env.settings.store, "write_text", broken)
    assert env.run() == 0  # a non-zero exit would make Cloud Run send the report twice
    assert "sent-marker could not be written" in env.telegram.text(ADMIN, "sendMessage")


# ---- 5.5 logs ---------------------------------------------------------------------
def test_logs_are_json_lines_with_severity_and_message(env):
    env.run()
    entries = env.log()
    assert entries and all({"severity", "message"} <= set(e) for e in entries)
    assert {e["severity"] for e in entries} <= {"INFO", "WARNING", "ERROR"}


# ---- 5.6 token reminder -----------------------------------------------------------
def test_reminder_when_fewer_than_45_days_remain():
    manifest = {"token_expires_at": "2026-11-20T10:00:00+08:00"}
    text = token_expiry_notice(manifest, datetime(2026, 10, 14, 4, 0, tzinfo=TZ))
    assert "20 Nov 2026" in text and "37 days" in text


def test_no_reminder_with_45_days_or_more():
    manifest = {"token_expires_at": "2026-11-28T04:00:00+08:00"}
    assert token_expiry_notice(manifest, datetime(2026, 10, 14, 4, 0, tzinfo=TZ)) is None
    assert token_expiry_notice({}, NOW) is None


def test_an_expired_token_is_called_expired():
    manifest = {"token_expires_at": "2026-10-01T00:00:00+08:00"}
    assert "expired on 1 Oct 2026" in token_expiry_notice(manifest, NOW)


def test_the_run_sends_the_reminder_to_the_admin_chat(env):
    env.manifest = {"token_expires_at": "2026-11-01T00:00:00+08:00", "warnings": []}
    assert env.run() == 0
    assert "Moka API token expires" in env.telegram.text(ADMIN, "sendMessage")
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]


def test_no_reminder_in_a_dry_run(tmp_path):
    env = Env(tmp_path, dry_run=True)
    env.manifest = {"token_expires_at": "2026-11-01T00:00:00+08:00", "warnings": []}
    env.run()
    assert env.telegram.calls == []
    assert any("Moka API token expires" in e["message"] for e in env.log())


# ---- Drive client -----------------------------------------------------------------
def test_drive_reuses_the_week_folder_and_replaces_files(tmp_path):
    class Session:
        def __init__(self):
            self.requests = []

        def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            if method == "GET":
                return _Response(200, {"files": [{"id": "existing"}]})
            return _Response(200, {"id": "x"})

    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    session = Session()
    url = DriveUploader(session, "root").upload_week("2026-10-13 (week 42)", [pdf])
    assert url == "https://drive.google.com/drive/folders/existing"
    assert [m for m, *_ in session.requests] == ["GET", "GET", "PATCH"]
    assert session.requests[2][1].endswith("/files/existing")
    assert session.requests[2][2]["data"] == b"%PDF"


def test_drive_escapes_quotes_in_names():
    session = DriveSession()
    DriveUploader(session, "root").ensure_folder("it's")  # a GET then a POST
    assert session.requests[0][0] == "GET"


def test_drive_error_text_names_the_reason():
    with pytest.raises(DriveError, match="storage quota"):
        DriveUploader(DriveSession(fail=True), "root").ensure_folder("x")


def test_drive_needs_a_folder_id():
    with pytest.raises(DriveError):
        DriveUploader(DriveSession(), "")


def test_week_folder_name():
    assert week_folder_name(date(2026, 11, 22)) == "2026-11-22 (week 47)"


# ---- stores -----------------------------------------------------------------------
def test_local_store_child_and_upload(tmp_path):
    store = LocalStore(tmp_path).child("dry-run")
    src = tmp_path / "a.txt"
    src.write_text("hi")
    store.upload("reports/x/a.txt", src)
    assert (tmp_path / "dry-run" / "reports" / "x" / "a.txt").read_text() == "hi"
    assert store.exists("reports/x/a.txt") and not store.exists("nope")
    assert store.url("raw").endswith("dry-run/raw")


class _Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_string(self, text, content_type=None):
        self.bucket.objects[self.name] = text

    def upload_from_filename(self, path):
        self.bucket.objects[self.name] = Path(path).read_text()

    def download_as_text(self):
        return self.bucket.objects[self.name]


class _Bucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return _Blob(self, name)


class _GcsClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, _Bucket())


def test_gcs_store_prefixes_keys_and_urls():
    client = _GcsClient()
    store = GcsStore("b", "", client).child("dry-run")
    store.write_text("sent/2026-10-13.json", "{}")
    assert "dry-run/sent/2026-10-13.json" in client.buckets["b"].objects
    assert store.exists("sent/2026-10-13.json")
    assert store.read_text("sent/2026-10-13.json") == "{}"
    assert store.url("raw") == "gs://b/dry-run/raw"
    assert GcsStore("b", "", client).url("raw") == "gs://b/raw"
