# How Hari Lari analyzes its Moka POS Sales data

Hari Lari is a small cafe in Bali. This repository holds the scripts we use to turn the raw
exports of our point-of-sale system, [Moka POS](https://www.mokapos.com/), into a cleaned data
set and a sales report. We share it so that other cafes on Moka can reuse our dimensions,
metrics and KPIs.

Our own sales data never leaves our machine: input files and generated reports are ignored by
git. The only data in this repository is a small made-up sample in `tests/fixtures/`.

## What it produces

The weekly report (`reports/weekly.yml`) reads the full sales history up to its anchor day and
writes these files to `data/output/reports/weekly/<anchor>/`:

- **`weekly_<anchor>.pdf`**: a landscape report made for reading on screen. The cover page shows the full-history totals and a summary card per section. The
  three sections are _last 14 days_ (by day), _last 6 complete ISO weeks_ (by week) and
  _last 3 complete months_ (by month), with three pages each: an overview (KPI cards, trend
  chart with week-over-week or month-over-month growth, key figures), adjusted categories with
  order penetration, category AOV and the AOV split into contributive AOVs per category (with
  a total line), and top items, orders by hour of day, payment types and discounts. Every
  section compares with the equally long period before it.
- **`weekly_<anchor>_cover.png`**: the cover page as a picture, for sending in a chat.
- **`weekly_<anchor>.xlsx`**: the cleaned sales lines as an Excel table for your own pivots,
  plus a sheet describing every column. Customer and staff names are removed.
- **`weekly_caption.txt`**: the Telegram caption under the cover, with the week's figures.
- **`bundle.json`**: every page and result as data, for a frontend (see "Reports" below).

The exact definition of every metric and dimension is in [METRICS.md](METRICS.md).

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and all dependencies:

```bash
git clone https://github.com/MortyTheMartian/hari-lari-analytics.git
cd hari-lari-analytics
uv sync
```

## Data source

The reports read a stored warehouse: `hari-lari-extract run` lands a Moka API extraction in
`data/landing/moka/<date>/`, and `hari-lari-warehouse build` builds the models from it and the
config into `data/warehouse/<date>/warehouse.duckdb`, which every report and component opens
read-only (`--warehouse`, default the newest build). No export has to be downloaded by hand,
and the item library is not needed: items are matched by their Moka id.

The two back-office exports (item library and sales _Item Details_) were the first source, read
by a pandas pipeline. For the 948 receipts and 1,700 lines up to 31 Oct 2026 both sources gave
identical lines. The pipeline was retired after four good Mondays; its results on the test
fixture are kept in `tests/golden/`, and the models are tested against them.

## Run

```bash
uv run hari-lari-extract run && uv run hari-lari-warehouse build    # land today's data, build the models
uv run hari-lari-report build --report weekly                       # newest warehouse, up to yesterday
uv run hari-lari-report build --report weekly --anchor 2026-11-15   # re-run an earlier week
```

Alternatively, `source setup.sh` activates the venv and loads variables from an optional,
git-ignored `.env` file; after that, `hari-lari-report` works without `uv run`. `--anchor
YYYY-MM-DD` sets the last day of the report; later sales are ignored, so a week can be rebuilt as
it looked then. It defaults to yesterday in Bali (the Monday report covers up to Sunday).

## Reports (component framework)

Reports are YAML files over the components in `src/hari_lari_analytics/presentation/`
(`docs/components.md` explains the whole framework). `hari-lari-report` builds them from the
stored warehouse; it never sends anything:

```bash
uv run hari-lari-warehouse build                              # data/landing -> data/warehouse/<today>/
uv run hari-lari-report validate                              # every report and component, the schemas
uv run hari-lari-report build --report tracer --anchor 2026-11-22   # PDF, PNG, text, XLSX, bundle.json
uv run hari-lari-report schedules                             # the reports on the tick, by cron
uv run hari-lari-pipeline tick --dry-run --date 2026-11-30    # the daily tick, without sending
```

The outputs go to `data/output/reports/<report>/<anchor>/` (`--out` changes it); `--strict` fails
when a text runs over its slot. To work on one component, draw it into the size of a slot, and
again on every save:

```bash
uv run hari-lari-component run category_contributive_aov --png --watch   # data/output/preview/<id>.png
```

`--span` (1 to 12 columns) and `--height` (1 to 6 parts of the page body) set the slot.

Two more outputs help to see what the framework can do and what it writes:

```bash
uv run hari-lari-report gallery            # every component on the latest run, one page each (data/output/gallery/)
uv run hari-lari-component manifest        # semantic_manifest.json: every metric, dimension, component, ...
```

The gallery is the visual check after a change to a visual, a transform or the theme. Every
build also writes `bundle.json`, the JSON results bundle: each page, slot and result as display
cells (raw value, display string, tone, small-sample flag) with the theme colours, so a frontend
can draw the report without computing anything. Its contract is
`src/hari_lari_analytics/presentation/schema/bundle.schema.json`, and a test checks every bundle
against it. `semantic_manifest.json` is the catalogue of the semantic layer (like dbt's
manifest); CI fails when it or the JSON Schemas are out of date (`uv run hari-lari-report schema`
and `uv run hari-lari-component manifest` rewrite them).

### Adding a metric, component, transform, visual or report

A new component is usually a YAML file in `src/hari_lari_analytics/presentation/components/`; a
new metric is a few lines in `semantic/metrics.yml`. The loop, from a similar component to a snapshot:

```bash
uv run hari-lari-component new orders_by_weekday --from category_contributive_aov
uv run hari-lari-component run orders_by_weekday --period L6W --sql   # the result and its SQL
uv run hari-lari-component run orders_by_weekday --png --watch        # a picture on every save
uv run pytest tests/test_snapshots.py --update-snapshots -k orders_by_weekday
uv run hari-lari-component validate && uv run hari-lari-report validate
```

The authoring guide is the Cursor rule `.cursor/rules/reporting-components.mdc` (it applies to
`semantic/`, `analysis/` and `presentation/`, and an LLM reads it as the procedure): how to add a metric, a
component, a transform, a visual and a report, the commands above and two worked examples, both
run as tests in `tests/test_authoring_examples.py`: a refund-rate metric with a component
(YAML only), and a transform in Python. The design note is `docs/components.md`.

## Report job

`hari-lari-report run --report weekly` runs the whole chain for one report (extract the Moka
API, check the data, build the report, store the files, upload to Google Drive, send to
Telegram); `--scheduled` runs every report with a `schedule` on one extraction. Useful options:
`--dry-run` (sends nothing), `--to admin` (private run to the admin chat), `--anchor
YYYY-MM-DD`, `--force`. In the cloud the daily tick runs them: `hari-lari-pipeline tick`
extracts, builds the warehouse and runs every report due that day (the weekly one on Mondays),
in the Cloud Run job `hari-lari-weekly`, started by the Cloud Scheduler job `hari-lari-tick` at
04:00 and 06:00 in Bali (the 06:00 tick retries what failed). The runbook (`docs/runbook.md`) explains the options, the files it writes and what each
alert means, and how a merge to `main` is released: `deploy/release.sh` builds the image, runs
it as a cloud dry run, only then points the live job at it and syncs the scheduler jobs
(GitHub Actions with Workload Identity Federation, see "Release path" there).

## Item aliases

Item names and categories change over time in Moka. `config/item_aliases.csv` maps old sales
item names to today's names and, where needed, overrides the category:

| Column              | Meaning                                                  |
| ------------------- | -------------------------------------------------------- |
| `sales_item`        | Item name exactly as it was sold                         |
| `adjusted_item`     | Today's item name (empty: keep the sales name)           |
| `adjusted_category` | Category override (empty: take it from the Moka catalog) |
| `note`              | Free text, ignored by the program                        |

The alias file wins when it lists a sales name; every other item follows its Moka id to today's
name and category (see [METRICS.md](METRICS.md)). So the file only needs rows for what an id
cannot tell: deleted items merged into a live one, add-ons that reuse an old item id, and
categories that Moka still has as `Uncategorized`. A renamed item needs no row.

## Using it for your own cafe

The code reads the Moka API with your own token (`hari-lari-extract`, see the runbook). The
business config is private and not in git: copy the demo files of `config/examples/` into
`config/` (or point `CONFIG_DIR` at another folder or a `gs://` location), replace
`item_aliases.csv` with your own mapping (or keep only its header) and run as above (with an old
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
  metric: more discounts or refunds show as bad. All colors live in the `Palette` of
  `presentation/visuals/theme.py`; visuals name colour tokens, so another palette re-themes the
  whole report.
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

Code lives in `src/hari_lari_analytics/`, in the modules of `docs/architecture.md`: `core/`
(settings and storage), `sources/moka/` (the Moka API extraction), `warehouse/` (models and
checks), `semantic/` (metrics and dimensions), `analysis/` (the compiler and transforms),
`presentation/` (components, reports and outputs), `orchestration/` (the commands that run a
report) and `distribution/` (Telegram and Drive). The framework's design is `docs/components.md`.

## License

[MIT](LICENSE)
