"""Command line entry point of the legacy report: `uv run hari-lari-report-legacy`.

It writes today's PDF and Excel from a CSV export or an API run. Plan 8.5 gave the name
`hari-lari-report` to the component report builder (`reporting/report_cli.py`); plan 9.7
deletes this command."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from hari_lari_analytics.clean import CleanResult, clean_sales
from hari_lari_analytics.dimensions import add_category_groups, add_dimensions
from hari_lari_analytics.load import (
    InputError,
    find_inputs,
    load_aliases,
    load_category_groups,
    load_library,
    load_sales,
)
from hari_lari_analytics.load_api import load_api_sales
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.project import project
from hari_lari_analytics.report_excel import write_excel
from hari_lari_analytics.report_pdf import write_pdf


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hari-lari-report-legacy",
        description="Clean the Moka POS data and write the PDF and Excel sales reports.",
    )
    parser.add_argument(
        "--source",
        choices=["csv", "api"],
        default="csv",
        help="csv: back-office exports in --input-dir; api: an extraction run in --extract-dir",
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/input"))
    parser.add_argument(
        "--extract-dir",
        default="data/extract",
        help="a run folder, or a folder (or gs://bucket/prefix) of dated runs; latest is used",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/output"))
    parser.add_argument("--aliases", type=Path, default=Path("config/item_aliases.csv"))
    parser.add_argument("--category-groups", type=Path, default=Path("config/category_group.csv"))
    parser.add_argument(
        "--anchor",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="last day of the report (default: yesterday, the anchor rule of project.yml, for "
        "--source api; the date of the last sale for --source csv); later sales are ignored",
    )
    return parser.parse_args(argv)


def load_lines(args: argparse.Namespace) -> tuple[CleanResult, list[str]]:
    """Cleaned lines of the chosen source, with the names of the sources for the cover."""
    aliases = load_aliases(args.aliases)
    if args.source == "api":
        run, result = load_api_sales(args.extract_dir, aliases)
        result.notes.extend(f"Extraction warning: {w}" for w in run.manifest.get("warnings", []))
        return result, [f"Moka API extraction {run.name}"]
    library_path, sales_path = find_inputs(args.input_dir)
    result = clean_sales(load_sales(sales_path), load_library(library_path), aliases)
    return result, [library_path.name, sales_path.name]


@dataclass(frozen=True)
class ReportFiles:
    pdf: Path
    cover_png: Path
    excel: Path


def prepare_lines(
    result: CleanResult, category_groups: Path, anchor: date | None, *, source: str
) -> tuple[pd.DataFrame, date]:
    """Add the dimensions and category groups, then cut the lines off after the anchor day.

    The default anchor is the anchor rule of project.yml (yesterday in the project's time zone)
    for the API and the last sale for the CSV files.
    """
    lines = add_dimensions(result.lines)
    lines, group_notes = add_category_groups(lines, load_category_groups(category_groups))
    result.notes.extend(group_notes)
    if anchor is None:
        anchor = project().default_anchor() if source == "api" else lines["sale_ts"].max().date()
    lines = lines[lines["sale_ts"] < pd.Timestamp(anchor + timedelta(days=1))]
    if lines.empty:
        raise InputError(f"There are no sales up to {anchor:%d %b %Y}")
    return lines, anchor


def write_reports(
    lines: pd.DataFrame,
    result: CleanResult,
    sources: list[str],
    anchor: date,
    out_dir: Path,
) -> ReportFiles:
    """Write the PDF, the cover PNG and the Excel lines export into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = ReportFiles(
        pdf=out_dir / f"hari_lari_sales_report_{anchor:%Y-%m-%d}.pdf",
        cover_png=out_dir / f"hari_lari_sales_cover_{anchor:%Y-%m-%d}.png",
        excel=out_dir / f"hari_lari_sales_lines_{anchor:%Y-%m-%d}.xlsx",
    )
    write_pdf(
        files.pdf,
        lines,
        report_periods(anchor),
        result.notes,
        sources,
        anchor,
        cover_png=files.cover_png,
    )
    write_excel(files.excel, lines)
    return files


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result, sources = load_lines(args)
        lines, anchor = prepare_lines(result, args.category_groups, args.anchor, source=args.source)
    except InputError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    out_dir = args.output_dir / f"{date.today():%Y-%m-%d}"
    files = write_reports(lines, result, sources, anchor, out_dir)

    total = compute_metrics(lines).iloc[0]
    print(
        f"Read {len(lines):,} sales lines ({lines['sale_ts'].min():%d %b %Y} – "
        f"{lines['sale_ts'].max():%d %b %Y}): {total.orders:,} orders, "
        f"{total.net_sales:,} {project().currency.code} net sales."
    )
    for note in result.notes:
        print(f"Note: {note}")
    print(f"Wrote {files.pdf}")
    print(f"Wrote {files.cover_png}")
    print(f"Wrote {files.excel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
