import json

import pytest

from hari_lari_analytics.deliver import cli, settings
from hari_lari_analytics.deliver.telegram import (
    MAX_CAPTION,
    TelegramClient,
    TelegramError,
    encode_multipart,
    format_alert,
    logs_url,
    scrub,
    send_alert,
)

TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake_12"
OK = (200, json.dumps({"ok": True, "result": {"message_id": 1}}).encode())


class FakeTransport:
    """Plays back a list of (status, payload) answers and records the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[tuple[str, bytes]] = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append((url, body))
        answer = self.answers.pop(0) if self.answers else OK
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def client(*answers):
    sleeps: list[float] = []
    transport = FakeTransport(*answers)
    return TelegramClient(TOKEN, transport=transport, sleep=sleeps.append), transport, sleeps


# ---- 4.7 client -------------------------------------------------------------------
def test_send_message_posts_to_the_method_url():
    c, transport, _ = client()
    c.send_message(-1001234, "hello")
    url, body = transport.requests[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert b'name="chat_id"\r\n\r\n-1001234' in body
    assert b"hello" in body


def test_photo_and_document_are_uploaded_as_files(tmp_path):
    cover, pdf = tmp_path / "cover.png", tmp_path / "report.pdf"
    cover.write_bytes(b"\x89PNG-data")
    pdf.write_bytes(b"%PDF-data")
    c, transport, _ = client()
    c.send_report(42, cover, pdf, "caption text")
    (photo_url, photo_body), (doc_url, doc_body) = transport.requests
    assert photo_url.endswith("/sendPhoto") and doc_url.endswith("/sendDocument")
    assert b'name="photo"; filename="cover.png"' in photo_body
    assert b"caption text" in photo_body
    assert b'name="document"; filename="report.pdf"' in doc_body
    assert b"Content-Type: application/pdf" in doc_body


def test_429_waits_for_retry_after_then_succeeds():
    flood = (
        429,
        {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}},
    )
    c, transport, sleeps = client(flood, OK)
    c.send_message(1, "x")
    assert len(transport.requests) == 2
    assert sleeps == [8]


def test_dropped_connection_and_5xx_are_retried():
    c, transport, sleeps = client(ConnectionResetError("reset"), (502, b"bad gateway"), OK)
    c.send_message(1, "x")
    assert len(transport.requests) == 3
    assert sleeps == [1, 2]


def test_gives_up_after_the_retries():
    c, transport, _ = client(*[(500, b"")] * 10)
    with pytest.raises(TelegramError, match="500"):
        c.send_message(1, "x")
    assert len(transport.requests) == 4


def test_client_error_is_not_retried_and_hides_the_token():
    bad = (400, {"ok": False, "description": f"Bad Request: chat not found {TOKEN}"})
    c, transport, sleeps = client(bad)
    with pytest.raises(TelegramError) as error:
        c.send_message(1, "x")
    assert len(transport.requests) == 1 and not sleeps
    assert "chat not found" in str(error.value)
    assert TOKEN not in str(error.value)


def test_network_error_text_hides_the_token():
    c, _, _ = client(*[OSError(f"failed {TOKEN}")] * 10)
    with pytest.raises(TelegramError) as error:
        c.send_message(1, "x")
    assert TOKEN not in str(error.value)


def test_50_mb_guard_stops_before_uploading(tmp_path, monkeypatch):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr("hari_lari_analytics.deliver.telegram.MAX_DOCUMENT_BYTES", 50)
    c, transport, _ = client()
    with pytest.raises(TelegramError, match="limit"):
        c.send_document(1, big)
    assert not transport.requests


def test_long_caption_is_cut_to_the_telegram_limit(tmp_path):
    cover = tmp_path / "c.png"
    cover.write_bytes(b"x")
    c, transport, _ = client()
    c.send_photo(1, cover, "a" * 3000)
    body = transport.requests[0][1]
    assert b"a" * MAX_CAPTION not in body and b"a" * (MAX_CAPTION - 1) in body


def test_empty_token_is_refused():
    with pytest.raises(TelegramError):
        TelegramClient("")


def test_multipart_has_one_part_per_field_and_file(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"data")
    body, content_type = encode_multipart({"a": "1"}, {"photo": f})
    boundary = content_type.split("boundary=")[1]
    assert body.count(f"--{boundary}\r\n".encode()) == 2
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_scrub_removes_anything_shaped_like_a_token():
    assert scrub(f"url bot{TOKEN}/send") == "url bot<token>/send"
    assert scrub("plain text") == "plain text"


# ---- 4.9 alerts -------------------------------------------------------------------
def test_alert_names_the_step_error_and_logs():
    text = format_alert("extract", RuntimeError(f"boom {TOKEN}"), "https://logs.example/x")
    assert "step: extract" in text
    assert "RuntimeError: boom <token>" in text
    assert "Logs: https://logs.example/x" in text


def test_alert_error_is_shortened():
    assert len(format_alert("report", "x" * 5000)) < 800


def test_send_alert_delivers_to_the_admin_chat():
    c, transport, _ = client()
    assert send_alert(c, 99, "deliver", ValueError("no"), "https://logs") is True
    assert b'name="chat_id"\r\n\r\n99' in transport.requests[0][1]


def test_send_alert_never_raises():
    c, _, _ = client(*[(400, {"ok": False, "description": "chat not found"})] * 2)
    assert send_alert(c, 99, "deliver", ValueError("no"), "https://logs") is False


def test_logs_url_points_at_the_execution_or_the_job():
    env = {"GCP_PROJECT": "p", "REGION": "r", "CLOUD_RUN_EXECUTION": "hari-lari-weekly-abc"}
    assert logs_url(env=env) == (
        "https://console.cloud.google.com/run/jobs/executions/details/r/"
        "hari-lari-weekly-abc/logs?project=p"
    )
    assert logs_url(env={}) == (
        "https://console.cloud.google.com/run/jobs/details/asia-southeast2/"
        "hari-lari-weekly/logs?project=hari-lari"
    )


# ---- CLI helpers ------------------------------------------------------------------
def test_chat_ids_lists_each_chat_once():
    updates = [
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "Moritz"}}},
        {"my_chat_member": {"chat": {"id": -100123, "type": "supergroup", "title": "Hari Lari"}}},
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "Moritz"}}},
    ]
    chats = cli.chats_from_updates(updates)
    assert list(chats) == [7, -100123]
    assert "-100123" in cli.describe_chat(chats[-100123])
    assert "Hari Lari" in cli.describe_chat(chats[-100123])


def test_secret_comes_from_the_environment_first(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " abc ")
    assert settings.get_secret(settings.BOT_TOKEN) == "abc"


def test_missing_secret_is_reported(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    failed = type("Result", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr("subprocess.run", lambda *a, **k: failed)
    with pytest.raises(settings.MissingSecret, match="telegram-chat-id"):
        settings.get_secret(settings.CHAT_ID)


def test_call_without_parameters_sends_an_empty_body():
    c, transport, _ = client()
    c.get_me()
    assert transport.requests[0][1] == b""
