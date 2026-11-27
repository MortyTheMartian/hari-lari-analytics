"""Fakes of the outside world for the job tests: a Telegram transport and a Drive session that
record what they are sent."""

from __future__ import annotations

import re

from hari_lari_analytics.distribution.telegram import TelegramClient

TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake_12"
GROUP, ADMIN = "-100111", "999"


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
            return Response(
                403, {"error": {"message": "Service Accounts do not have storage quota"}}
            )
        if method == "GET":
            return Response(200, {"files": []})
        return Response(200, {"id": f"id{next(self.ids)}"})


class Response:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload
