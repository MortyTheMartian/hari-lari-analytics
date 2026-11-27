"""The PNG pages: chosen pages at a resolution that reads well on a phone."""

from __future__ import annotations

from pathlib import Path

from matplotlib.figure import Figure

PNG_DPI = 150  # 1753 x 1240 px: sharp on a phone, far below Telegram's photo limits


def write_png(path: Path, fig: Figure) -> Path:
    fig.savefig(path, dpi=PNG_DPI)
    return path
