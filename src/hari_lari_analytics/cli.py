"""Command line entry point: `uv run hari-lari-report`."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from hari_lari_analytics.clean import clean_sales
from hari_lari_analytics.dimensions import add_dimensions
from hari_lari_analytics.load import (
    InputError,
    find_inputs,
    load_aliases,
    load_library,
    load_sales,
)
from hari_lari_analytics.metrics import compute_metrics
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.report_excel import write_excel
from hari_lari_analytics.report_pdf import write_pdf


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hari-lari-report",
        description="Clean the Moka POS exports and write the PDF and Excel sales reports.",
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/input"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/output"))
    parser.add_argument("--aliases", type=Path, default=Path("config/item_aliases.csv"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        library_path, sales_path = find_inputs(args.input_dir)
        result = clean_sales(
            load_sales(sales_path), load_library(library_path), load_aliases(args.aliases)
        )
    except InputError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    lines = add_dimensions(result.lines)
    anchor = lines["sale_ts"].max().date()
    out_dir = args.output_dir / f"{date.today():%Y-%m-%d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"hari_lari_sales_report_{anchor:%Y-%m-%d}.pdf"
    excel_path = out_dir / f"hari_lari_sales_lines_{anchor:%Y-%m-%d}.xlsx"

    write_pdf(
        pdf_path, lines, report_periods(anchor), result.notes, [library_path.name, sales_path.name]
    )
    write_excel(excel_path, lines)

    total = compute_metrics(lines).iloc[0]
    print(
        f"Read {len(lines):,} sales lines ({lines['sale_ts'].min():%d %b %Y} – "
        f"{lines['sale_ts'].max():%d %b %Y}): {total.orders:,} orders, "
        f"{total.net_sales:,} IDR net sales."
    )
    for note in result.notes:
        print(f"Note: {note}")
    print(f"Wrote {pdf_path}")
    print(f"Wrote {excel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
