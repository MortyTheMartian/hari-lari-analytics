# How Hari Lari analyzes its Moka POS Sales data

Hari Lari is a small cafe in Bali. This repository holds the scripts we use to turn the raw
exports of our point-of-sale system, [Moka POS](https://www.mokapos.com/), into a cleaned data
set and a sales report. We share it so that other cafes on Moka can reuse our dimensions,
metrics and KPIs.

Our own sales data never leaves our machine: input files and generated reports are ignored by
git. The only data in this repository is a small made-up sample in `tests/fixtures/`.

## What it produces

Every run reads the full sales history and writes two files to `data/output/<run date>/`:

- **`hari_lari_sales_report_<last sale date>.pdf`**: a cover page plus three sections,
  *last 14 days* (by day), *last 6 complete ISO weeks* (by week) and *last 3 complete months*
  (by month). Each section compares with the equally long period before it and shows key
  figures, a trend chart, adjusted categories with order penetration, top items, payment
  types, discounts and orders by hour of day.
- **`hari_lari_sales_lines_<last sale date>.xlsx`**: the cleaned sales lines as an Excel table
  for your own pivots, plus a sheet describing every column. Customer and staff names are
  removed.

The exact definition of every metric and dimension is in [METRICS.md](METRICS.md).

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and all dependencies:

```bash
git clone https://github.com/<owner>/hari-lari-analytics.git
cd hari-lari-analytics
uv sync
```

## Input files

Export two files from the Moka back office and put them in `data/input/`, keeping exactly one
of each (replace the old ones, every run uses the full history):

| File | Moka export | Name pattern |
| --- | --- | --- |
| Item library | Item library export | `YYYY_MM_DD_item_library.csv` |
| Sales | Sales report *Item Details*, full date range | `YYYY_MM_DD_sales_report_item_details_DD-MM-YYYY_DD-MM-YYYY.csv` |

`YYYY_MM_DD` is the export date; the sales file also carries the exported date range.

## Run

```bash
uv run hari-lari-report
```

Alternatively, `source setup.sh` activates the venv and loads variables from an optional,
git-ignored `.env` file; after that, `hari-lari-report` works without `uv run`.

Options: `--input-dir`, `--output-dir` and `--aliases` override the default paths. The run
prints a note for every item name it could not match to today's item library.

## Item aliases

Item names and categories change over time in Moka. `config/item_aliases.csv` maps old sales
item names to today's names and, where needed, overrides the category:

| Column | Meaning |
| --- | --- |
| `sales_item` | Item name exactly as it appears in the sales export |
| `adjusted_item` | Today's item name (empty: keep the sales name) |
| `adjusted_category` | Category override (empty: take it from the item library) |
| `note` | Free text, ignored by the program |

Items that are neither in the library nor in the alias file fall back to their most recent
category in the sales export and are listed in the run output and on the PDF cover page.

## Using it for your own cafe

The code expects the column layout of Moka's *Item Details* export. Replace
`config/item_aliases.csv` with your own mapping (or empty it) and run as above.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Code lives in `src/hari_lari_analytics/`: `load.py` (find and validate inputs), `clean.py`
(tidy lines, refunds, adjusted items and categories), `dimensions.py`, `metrics.py`,
`periods.py`, `report_pdf.py`, `report_excel.py` and `cli.py`.

## License

[MIT](LICENSE)
