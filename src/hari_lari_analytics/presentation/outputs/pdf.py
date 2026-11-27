"""The PDF: one A4 landscape page per page of the report."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure


def write_pdf(path: Path, figures: Sequence[Figure], title: str) -> Path:
    with PdfPages(path) as pdf:
        for fig in figures:
            pdf.savefig(fig)
        pdf.infodict()["Title"] = title
    return path
