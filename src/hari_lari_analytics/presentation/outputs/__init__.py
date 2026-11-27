"""The outputs of a report: PDF, PNG pages, Telegram text, workbook and JSON bundle."""

from __future__ import annotations

from pathlib import Path

import matplotlib

from hari_lari_analytics.presentation.builder import BuildResult, ReportRun
from hari_lari_analytics.presentation.layouts import Overflow
from hari_lari_analytics.presentation.outputs import pages
from hari_lari_analytics.presentation.outputs.bundle import write_bundle
from hari_lari_analytics.presentation.outputs.pdf import write_pdf
from hari_lari_analytics.presentation.outputs.png import write_png
from hari_lari_analytics.presentation.outputs.telegram import write_text
from hari_lari_analytics.presentation.outputs.xlsx import write_workbook
from hari_lari_analytics.presentation.visuals.theme import DEFAULT_THEME, Theme


def png_pages(run: ReportRun) -> list[str]:
    """The ids of the pages that are written as PNG: `outputs.png` lists page ids, and the id
    of a repeated page stands for all of its repeats."""
    wanted = run.report.spec.outputs.png
    ids = [p.page.id for p in run.pages if p.page.empty_message is None]
    if wanted == "all":
        return ids
    return [p.page.id for p in run.pages if p.page.base in wanted and p.page.empty_message is None]


def write_outputs(run: ReportRun, out_dir: Path, theme: Theme = DEFAULT_THEME) -> BuildResult:
    """Write the outputs a report asks for into `out_dir`, and the JSON bundle always."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = run.report.spec
    stem = f"{spec.id}_{run.anchor:%Y-%m-%d}"
    overflows: list[Overflow] = []
    wanted_png = set(png_pages(run))
    pdf = xlsx = None
    pngs: dict[str, Path] = {}
    with matplotlib.rc_context(theme.rc()):
        figures = []
        for page in run.pages:
            fig, found = pages.render_page(run, page, theme)
            overflows += found
            figures.append(fig)
            if page.page.id in wanted_png:
                pngs[page.page.id] = write_png(out_dir / f"{stem}_{page.page.id}.png", fig)
        if spec.outputs.pdf:
            title = f"{spec.title} through {run.anchor:%d %b %Y}"
            pdf = write_pdf(out_dir / f"{stem}.pdf", figures, title)
    if spec.outputs.xlsx:
        sheets = [(s.ref.bound.id, s.ref.bound.query, s.facets) for s in run.sheets]
        xlsx = write_workbook(out_dir / f"{stem}.xlsx", sheets)
    texts = {name: write_text(out_dir / f"{name}.txt", text) for name, text in run.texts.items()}
    bundle = write_bundle(out_dir / "bundle.json", run, theme)
    return BuildResult(run, out_dir, pdf, pngs, xlsx, bundle, texts, overflows)
