"""Telegram Bot API client: text, photo and document messages, plus the admin alert.

Standard library only. The bot token sits inside every request URL, so it is scrubbed from
every error text and log line that leaves this module.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

API = "https://api.telegram.org"
TIMEOUT_SECONDS = 120
MAX_RETRIES = 3
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # Bot API limit for an uploaded file
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Bot API limit for an uploaded photo
MAX_CAPTION = 1024
MAX_TEXT = 4096
MAX_ALERT_ERROR = 600

TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")

# transport(url, body, headers, timeout) -> (http status, response body)
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class TelegramError(RuntimeError):
    """A failed Bot API call. The message never contains the bot token."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def scrub(text: str, token: str | None = None) -> str:
    """Remove the bot token (or anything shaped like one) from a text."""
    if token:
        text = text.replace(token, "<token>")
    return TOKEN_PATTERN.sub("<token>", text)


def _urllib_transport(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:  # Telegram explains failures in the JSON body
        return error.code, error.read()


def encode_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type header."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + str(value).encode()
            + b"\r\n"
        )
    for name, path in files.items():
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode()
            + path.read_bytes()
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport = _urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if not token:
            raise TelegramError("The Telegram bot token is empty")
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries

    # ---- low level ---------------------------------------------------------------
    def call(
        self,
        method: str,
        fields: dict[str, str] | None = None,
        files: dict[str, Path] | None = None,
    ):
        """Call a Bot API method and return its `result`.

        A 429 waits for `retry_after`; 5xx and dropped connections back off. Anything else
        (bad chat id, kicked bot, ...) raises at once.
        """
        url = f"{API}/bot{self._token}/{method}"
        if fields or files:
            body, content_type = encode_multipart(fields or {}, files or {})
            headers = {"Content-Type": content_type}
        else:  # an empty multipart body is a 400 for methods without parameters
            body, headers = b"", {}
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                status, raw = self._transport(url, body, headers, TIMEOUT_SECONDS)
            except OSError as error:
                if last:
                    raise TelegramError(f"{method}: {scrub(str(error), self._token)}") from None
                self._sleep(2**attempt)
                continue
            payload = _json(raw)
            if status == 200 and payload.get("ok"):
                return payload["result"]
            description = scrub(str(payload.get("description", raw[:200])), self._token)
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if status == 429 and not last:
                self._sleep(int(retry_after or 1) + 1)
                continue
            if status >= 500 and not last:
                self._sleep(2**attempt)
                continue
            raise TelegramError(
                f"{method} failed ({status}): {description}", status=status, retry_after=retry_after
            )
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- messages ----------------------------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self) -> list[dict]:
        return self.call("getUpdates", {"timeout": "0"})

    def send_message(self, chat_id: str | int, text: str) -> dict:
        return self.call(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text[:MAX_TEXT],
                "disable_web_page_preview": "true",
            },
        )

    def send_photo(self, chat_id: str | int, path: Path, caption: str = "") -> dict:
        _guard_size(path, MAX_PHOTO_BYTES, "photo")
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = _caption(caption)
        return self.call("sendPhoto", fields, {"photo": path})

    def send_document(self, chat_id: str | int, path: Path, caption: str = "") -> dict:
        _guard_size(path, MAX_DOCUMENT_BYTES, "document")
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = _caption(caption)
        return self.call("sendDocument", fields, {"document": path})

    def send_report(self, chat_id: str | int, cover_png: Path, pdf: Path, caption: str) -> None:
        """The weekly report: the cover as a photo with the caption, then the PDF."""
        self.send_photo(chat_id, cover_png, caption)
        self.send_document(chat_id, pdf)


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _guard_size(path: Path, limit: int, kind: str) -> None:
    size = Path(path).stat().st_size
    if size > limit:
        raise TelegramError(
            f"{Path(path).name} is {size / 1e6:.1f} MB, over the {limit / 1e6:.0f} MB "
            f"Telegram limit for a {kind}"
        )


def _caption(text: str) -> str:
    if len(text) <= MAX_CAPTION:
        return text
    return text[: MAX_CAPTION - 1].rstrip() + "…"


# ---- admin alert (4.9) ------------------------------------------------------------
def logs_url(project: str | None = None, region: str | None = None, env: dict | None = None) -> str:
    """A link to the logs of the running Cloud Run execution (or of the job, when unknown)."""
    env = os.environ if env is None else env
    project = project or env.get("GCP_PROJECT") or "hari-lari"
    region = region or env.get("REGION") or "asia-southeast2"
    base = "https://console.cloud.google.com/run/jobs"
    execution = env.get("CLOUD_RUN_EXECUTION")
    if execution:
        return f"{base}/executions/details/{region}/{execution}/logs?project={project}"
    job = env.get("CLOUD_RUN_JOB") or "hari-lari-weekly"
    return f"{base}/details/{region}/{job}/logs?project={project}"


def format_alert(step: str, error: BaseException | str, link: str | None = None) -> str:
    message = scrub(str(error) or type(error).__name__)
    if len(message) > MAX_ALERT_ERROR:
        message = message[: MAX_ALERT_ERROR - 1].rstrip() + "…"
    kind = f"{type(error).__name__}: " if isinstance(error, BaseException) else ""
    lines = [f"⚠️ Hari Lari weekly report failed at step: {step}", "", f"{kind}{message}"]
    if link:
        lines += ["", f"Logs: {link}"]
    return "\n".join(lines)


def send_alert(
    client: TelegramClient,
    admin_chat_id: str | int,
    step: str,
    error: BaseException | str,
    link: str | None = None,
) -> bool:
    """Tell the admin chat which step failed. Never raises: an alert must not hide the real error.

    Returns whether the alert was delivered.
    """
    try:
        client.send_message(admin_chat_id, format_alert(step, error, link or logs_url()))
    except Exception:  # noqa: BLE001
        return False
    return True


def send_notice(client: TelegramClient, admin_chat_id: str | int, text: str) -> bool:
    """A plain message to the admin chat (a warning or a reminder). Never raises."""
    try:
        client.send_message(admin_chat_id, scrub(text))
    except Exception:  # noqa: BLE001
        return False
    return True
