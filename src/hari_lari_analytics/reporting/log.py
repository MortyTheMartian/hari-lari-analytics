"""Structured JSON logging. Cloud Logging reads `severity` and `message` from a JSON line."""

from __future__ import annotations

import json
import sys
from typing import TextIO


class JsonLog:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream

    def _write(self, severity: str, message: str, fields: dict) -> None:
        entry = {"severity": severity, "message": message, **fields}
        print(json.dumps(entry, ensure_ascii=False, default=str), file=self.stream or sys.stdout,
              flush=True)  # fmt: skip

    def info(self, message: str, **fields) -> None:
        self._write("INFO", message, fields)

    def warning(self, message: str, **fields) -> None:
        self._write("WARNING", message, fields)

    def error(self, message: str, **fields) -> None:
        self._write("ERROR", message, fields)
