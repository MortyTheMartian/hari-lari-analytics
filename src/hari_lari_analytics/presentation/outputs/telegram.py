"""The Telegram text: plain text per block, with the Drive link filled in at delivery.

The builder writes each block of `outputs.telegram` with `{{ drive_url }}` still in it (the link
exists only after the upload); `fill_drive_url` is what the deliver step calls.
"""

from __future__ import annotations

from pathlib import Path

from hari_lari_analytics.presentation.reports import DRIVE_PLACEHOLDER


def write_text(path: Path, text: str) -> Path:
    path.write_text(text + "\n")
    return path


def fill_drive_url(text: str, url: str | None) -> str:
    """The text with the Drive link in place of the placeholder; without a link (the upload
    failed) the lines that use it are left out, so no message shows a bare placeholder."""
    if url:
        return text.replace(DRIVE_PLACEHOLDER, url)
    kept = [line for line in text.split("\n") if DRIVE_PLACEHOLDER not in line]
    return "\n".join(kept).strip("\n")
