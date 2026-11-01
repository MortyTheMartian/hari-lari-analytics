# How Hari Lari analyzes its Moka POS Sales data

Hari Lari is a small cafe in Bali. This repository holds the scripts we use to turn the raw
exports of our point-of-sale system, [Moka POS](https://www.mokapos.com/), into a cleaned data
set and a sales report. We share it so that other cafes on Moka can reuse our dimensions,
metrics and KPIs.

Our own sales data never leaves our machine: input files and generated reports are ignored by
git. The only data in this repository is a small made-up sample in `tests/fixtures/`.

## What it produces

Every run reads the full sales history and writes two files to `data/output/<run date>/`:

- **`hari_lari_sales_report_<last sale date>.pdf`**: a landscape report made for reading on
  screen. The cover page shows the full-history totals and a summary card per section. The
  three sections are _last 14 days_ (by day), _last 6 complete ISO weeks_ (by week) and
  _last 3 complete months_ (by month), with three pages each: an overview (KPI cards, trend
  chart with week-over-week or month-over-month growth, key figures), adjusted categories with
  order penetration, and top items, orders by hour of day, payment types and discounts. Every
  section compares with the equally long period before it.
- **`hari_lari_sales_lines_<last sale date>.xlsx`**: the cleaned sales lines as an Excel table
  for your own pivots, plus a sheet describing every column. Customer and staff names are
  removed.

The exact definition of every metric and dimension is in [METRICS.md](METRICS.md).

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and all dependencies:

```bash
git clone https://github.com/MortyTheMartian/hari-lari-analytics.git
cd hari-lari-analytics
uv sync
```

## Input files

Export two files from the Moka back office and put them in `data/input/`, keeping exactly one
of each (replace the old ones, every run uses the full history):

| File         | Moka export                                  | Name pattern                                                     |
| ------------ | -------------------------------------------- | ---------------------------------------------------------------- |
| Item library | Item library export                          | `YYYY_MM_DD_item_library.csv`                                    |
| Sales        | Sales report _Item Details_, full date range | `YYYY_MM_DD_sales_report_item_details_DD-MM-YYYY_DD-MM-YYYY.csv` |

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

| Column              | Meaning                                                  |
| ------------------- | -------------------------------------------------------- |
| `sales_item`        | Item name exactly as it appears in the sales export      |
| `adjusted_item`     | Today's item name (empty: keep the sales name)           |
| `adjusted_category` | Category override (empty: take it from the item library) |
| `note`              | Free text, ignored by the program                        |

Items that are neither in the library nor in the alias file fall back to their most recent
category in the sales export and are listed in the run output and on the PDF cover page.

## Using it for your own cafe

The code expects the column layout of Moka's _Item Details_ export. Replace
`config/item_aliases.csv` with your own mapping (or empty it) and run as above (with an old
itemized sales report from Moka POS, any LLM can quickly draft the item_aliases.csv for you).

## Report design

The PDF is made for reading on screen, not for printing. These are the choices behind it:

- **Layout.** A4 landscape pages with a light grey background and white rounded cards, one
  card per question (How much did we sell? Which categories? When?). Every page has the same
  header: a page title on the left and pill-shaped chips on the right for the period, its
  dates and the period it is compared with.
- **Colors.** One accent color carries the meaning throughout: teal for the current period
  and for good changes, coral for the prior period and for highlights (such as the three
  busiest hours), and orange-red for bad changes. Whether a change is good depends on the
  metric: more discounts or refunds show as bad. All colors live in the `Palette` at the top
  of `report_pdf.py`; passing another palette to `PdfReport` re-themes the whole report.
- **Font.** [Inter](https://rsms.me/inter/), bundled in `src/hari_lari_analytics/fonts/`
  under the SIL Open Font License (`Inter-OFL.txt`) and embedded in the PDF, so the report
  looks the same on every machine.
- **Comparisons first.** We just opened, so growth matters most. KPI cards and key figures
  always show the change against the equally long period before, the trend bars mark the
  prior period with a tick at the same position, and the weekly and monthly trends label
  every bar with its week-over-week or month-over-month growth. The daily trend has no growth
  labels, because day-to-day swings mostly reflect the weekday. Where there is not enough
  history, the report says "n/a" or "no comparison" instead of comparing with a partial
  period.
- **Small line charts.** The KPI cards show a line per day for the last 14 days and per week
  for the longer periods, so the long views show the trend rather than daily noise.
- **Honest numbers.** Orders are counted from the receipts for every view, never summed
  across days, weeks or categories, because one receipt can span several categories. When
  there are more than four payment types, the top three are shown and the rest grouped into
  "Other"; its orders may be added up, because a receipt has exactly one payment type.
  Extreme changes are capped at ">+999%" so they don't swamp the layout.

**Tools.** Everything is drawn with matplotlib, with
[plottable](https://github.com/znstrider/plottable) for the tables with in-cell bars. We also
tried [Great Tables](https://posit-dev.github.io/great-tables/) with Plotly, Altair and
plotnine, rendered to PDF through HTML. The results looked similar, but needed a headless
browser or an HTML-to-PDF engine and took far longer to build (around 100 seconds instead of
about 10), so we stayed with plain matplotlib: pure Python, fast, vector output.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Code lives in `src/hari_lari_analytics/`: `load.py` (find and validate inputs), `clean.py`
(tidy lines, refunds, adjusted items and categories), `dimensions.py`, `metrics.py`,
`periods.py`, `report_data.py` (the figures of each report section), `report_pdf.py` (the
layout, see [Report design](#report-design)), `report_excel.py` and `cli.py`.

## License

[MIT](LICENSE)
