# Framework gaps

What the extensibility trial (plan 9.1) and the later tasks found where a component or report
needed more than YAML, or where YAML needed a workaround. Each gap is closed in the framework
(plan 9.2) rather than worked around in a component; the status says how.

## Trial 1 (9.1, 25 Nov 2026)

Rules: each component time-boxed to 30 minutes, YAML only; every point where Python was needed is
written down. Measured on the run of 22 Nov 2026 with `hari-lari-component run` and `--png`.

| Component | Time | YAML only | Notes |
| --- | --- | --- | --- |
| `orders_by_weekday_hour`: orders per trading day by weekday and hour (`fct_orders`, `per_trading_day`, `pivot`, `heatmap`) | 4 min | yes | First run right. |
| `aov_basket_bridge`: AOV split into items per order and avg item price (`decompose`, `waterfall`) | 3 min | yes | Reproduces the worked example of `docs/components.md` exactly (+3,812 and +3,549). |
| `attach_rate_trend`: food and drink attach rates per week (filtered metrics on `fct_orders`, `bar_trend`, facet by metric) | 2 min | yes | One loader error (`movers` reads rows, not a series) named the fix. |
| `stock_alert` (added by you): the stock-tracked item-variants at or below their alert-at quantity, with the stock level and status, plus the report `stock_alert` | 4 min | **no** | Python twice (G1), three workarounds (G2, G3, G4). |

All four took about 13 minutes together, far inside the 30 minutes each. Two caveats on the
measure. First, the heatmap and the AOV bridge existed already as test fixtures
(`tests/fixtures/components/`), written in phase 7 to test the transforms, so those two mostly
measured that the packaged registries hold what the fixtures assumed. Second, the stock alert was
the only component on data the framework had never modelled, and it is the one that found the
gaps: the framework is complete for new views of the sales, not yet for a new kind of data.

### Where Python was needed

- **G1. A new Moka table needs Python in two places.** The stock levels are in the run's
  `item_variants` table (`in_stock`, `stock_alert`, `track_stock`, `alert`), which no model read.
  A model reading `raw_item_variants` needed (a) an entry with column types in `RAW_SOURCES` of
  `reporting/db.py` and (b) the table name in `OPTIONAL_TABLES` of `load_api.py`, which decides
  what `load_run` reads. Without (a), every component failed with a bare DuckDB traceback
  ("Table with name raw_item_variants does not exist"), not only the new one; with (a) but not
  (b), the optional table was silently empty and the component showed "no rows".

### Workarounds in YAML or SQL

- **G2. A snapshot model cannot be queried** (structural). Every query needs a `window` on the
  model's `time` column, and a model without one is refused ("has no time column in models.yml,
  so no window applies to it"). Stock is a snapshot without dates. Workaround: `fct_stock` got
  a fake `stock_date` equal to the anchor, `time: stock_date` and the query `window: full`, so
  the result carries a meaningless window (23 Jul to the anchor).
- **G3. A dimension's sort metric must exist on every model of the dimension.**
  `adjusted_item_variant` sorts by `net_sales`, which `fct_stock` does not have, and a query cannot
  sort otherwise. The error ("metric net_sales is not on fct_stock") did not say that it came
  from the dimension's sort, and the registry let the dimension onto the model without
  complaint. Workaround: a second dimension `stock_item_variant` on the same column, sorted by
  value.
- **G4. `table` has no text form**, so a table cannot be a line of a Telegram text block.
  Workaround: the text block reuses the component with `with: { visual: notes_card ... }`.

### Smaller findings

- **G5. A snapshot cannot say when it was taken.** The stock is Moka's count at extraction time,
  but no template can show that time: the report's chrome says "data through <anchor>", and the
  anchor defaults to yesterday while the extraction may be older (the report of 25 Nov showed the
  stock of 22 Nov as "data of 2026-11-24").
- **G6. `sort: { by: value }` sorts the text**, not in the order of the dimension's `values`, so
  the stock statuses sort as low, negative, out instead of negative, out, low.
- **G7. A status is not coloured.** The table cannot shade a dimension value (Negative, Low) with a
  tone; only changes carry tones.
- **G8. The growth labels of a rate are ratios.** `growth_vs_previous` divides, so the attach rate
  from 32.1% to 37.0% reads "+15.3%", where "+4.9 pp" is what a reader expects (the `_delta` of a
  rate is already in pp).
- **G9. In `bar_trend` the value labels collide with the prior-period ticks** when the prior value
  is just above the bar (the food attach rate in W42 and W44).
- **G10. In `waterfall` the "Prior" bar is drawn in the secondary colour** (coral), which on the
  daylight theme can read as "bad".
- **Not a framework gap:** the live job extracts once a week, so a stock alert from it is a weekly
  snapshot. A real alert needs its own schedule and an extraction before it (`hari-lari-report run`
  with `schedule` and `deliver.when: rows`, plan 9.7 and 10.17).

## Status (9.2)

| Gap | Status | How |
| --- | --- | --- |
| G1 | closed | The run tables are declared in `reporting/sources.yml` (columns, types, `required`); `load_run` reads the tables listed there, so a new Moka table is one YAML entry and a `stg_` model. A model that reads an undeclared `raw_` table, an unknown `seed_` or a missing model fails the build naming the model and the table, instead of DuckDB's traceback. |
| G2 | closed | A model without `time` is a snapshot: its query names no window and no comparison (both refused with a sentence that says why), and a timed model's query must name its window. `fct_stock` lost its fake date. |
| G3 | closed | A dimension whose sort metric is not on the queried model sorts by its value; the duplicate dimension is gone. |
| G4 | closed | `table` has a text form (one line per row; without dimensions, one per metric with its change); the stock text block uses the component as it is. |
| G5 | closed | `run_context.extracted_at` is the run's extraction time (manifest `run_at`, local clock); `fct_stock.counted_at` carries it and the metric `stock_counted_at` shows it, so the report and its text say "Moka stock counted at 22 Nov 14:49" through an ordinary `text_line` component (`stock_counted_at`). |
| G6 | closed | A dimension with `values` sorts in their order (negative, out, low, ok). |
| G7 | open | Shading a dimension value by tone is a new table option; the order and the status label carry the meaning for now. Left for when a second component needs it. |
| G8 | closed | `growth_vs_previous` gives the difference for a `pct` metric and the text shows it in pp (+4.9 pp). |
| G9 | closed | The value and growth labels of `bar_trend` sit above the bar or its prior tick, whichever is higher. |
| G10 | open | Design, not a gap: the theme draws every prior value in the secondary colour (also the prior ticks of today's report); changing it is a theme decision for the owner. |

All stock components and the report are YAML only now: `stock_alert` (the table),
`stock_counted_at` (the as-of line) and the report `reports/stock_alert.yml` with its Telegram
text. The test fixture has an `item_variants` table (one variant per status, an untracked and a
deleted one), so the snapshots and tests cover the stock.

### Trial 2 (9.2, 25 Nov 2026)

G2 was structural, so one more new component on the snapshot model, YAML only:

| Component | Time | YAML only | Notes |
| --- | --- | --- | --- |
| `stock_by_status`: stock-tracked item-variants per status (a new `count(*)` metric `stock_variants` on `fct_stock`, `hbar`) | 1 min | yes | First run right and in status order; `fill_domain` added the "Out of stock 0" bar. Placed next to the table on the stock page. |

## Port of today's report (9.3, 25 Nov 2026)

Every piece of today's report is a packaged component now, and `tests/test_port.py` checks each
one's result against `report_data.Section` (or the cover's own figures) for L14D, L6W and L3M, on
the fixture run (where L14D has no comparison and L3M no sales) and on the real run of 22 Nov:
`full_history_kpis`, `full_history_amounts`, `last_sale`, `period_summaries`, `data_notes`,
`period_kpis`, `sales_trend`, `key_figures`, `category_contributive_aov`,
`category_penetration`, `top_items`, `orders_by_hour`, `payment_types`, `discount_codes` and
`lines_export`. The page chips, the footer and the empty-period page belong to the report, not
to a component, and are checked with `reports/weekly.yml` (9.4).

What the port needed beyond YAML:

| Gap | Status | How |
| --- | --- | --- |
| P1. A facet by period cannot change the grain, so one component could not draw today's sparklines (by day for L14D, by 7-day block otherwise) | closed | `grain: spark` in a query: `day` on a daily window, `block_7d` otherwise (compiler). |
| P2. `top: {n: 3, other: true}` turned a fourth payment type into "Other (1)", where today's report shows all four | closed | "Other" never hides a single group: when it would hold one group of the window and no group of the prior window or the buckets before, that group keeps its name (compiler). |
| P3. The summary card said "by week" for L3M, from its sparkline's 7-day blocks | closed | The card names the period's own grain, as today ("by month"). |
| P4. A fully refunded receipt adds an hour with 0 orders to "Orders by hour" | closed in YAML | A dimension `counts_as_order` and `where: { counts_as_order: true }`. |
| P5. The cover's note "Last sale 22 Nov 2026 11:58" has no place: a page note is only the "no comparison" note | open | The component `last_sale` (a `text_line`) shows it on the page instead. A page `note` template would be the general form. |
| P6. A hint cannot name the currency, so `sales_trend` and `key_figures` write "IDR" | open | Small: the currency in the template context of titles and hints. Matters for the publishable repository (phase 11), not for the café. |
| P7. The orders line under the trend has no growth labels (today's has them) | open | Drawing only; the numbers are in the result. |

Differences that are improvements, not gaps: today's cleaning notes (unmatched items, unmapped
categories) are data-check warnings now, worded by the checks; small samples are drawn pale
(hours with few orders, categories in few orders); the line "Metric definitions: see
METRICS.md" is not repeated on every cover.

## The weekly report (9.4, 25 Nov 2026)

`reports/weekly.yml` has today's ten pages in today's order (the cover, then overview,
categories and details for L14D, L6W and L3M, an empty period as one "Overview" page) and the
caption `weekly_caption`. `tests/test_port.py` checks the page titles, chips, notes, footer, the
empty page and the caption against `report_pdf.py` and `deliver/caption.py`, on the fixture and
the real run. What it needed:

| Gap | Status | How |
| --- | --- | --- |
| W1. The comparison chip said "vs prior 14 days" where today's names the days | closed | The chip shows the comparison window's days ("vs 26 Oct 2026 – 08 Nov 2026"). |
| W2. A text line could not write the caption's week ("Week 47 · 16–22 Nov 2026", and the week before) | closed | `anchor_week` in the template context and the filter `days_ending(n, before=0)`; `date_range` moved from `deliver/caption.py` to `formats.py`. |
| W3. The details page does not fit today's layout (hours, payments and discounts stacked on the right) with the visuals' minimum sizes | accepted | Two rows: top items and hours, then discounts and payments. |
| W4. The categories page loses today's paragraph on reading a change in contributive AOV: a page cannot hold a static text | open | A `text` slot (a template, no component) would carry it; the formula is in the card's hint. |

The caption has today's numbers; its form is the framework's: "Net Sales: 13,752,000 IDR
(+3.9% vs prior 7 days)" for "Net sales: IDR 13,752,000 (▲ 3.9%)", a blank line between its
parts, and under the sample-size rule (fewer than 30 orders in the week) no change but "(n=…)".

## Category deep dive (9.5, 25 Nov 2026)

`reports/category_deep_dive.yml` is YAML only: one page per category of the top 5 by net sales
over L6W (`repeat` with `top`), the category as a `focus`, and four packaged components with
`with:` overrides (`sales_trend` for penetration and category AOV, `top_items`,
`orders_by_hour`, `category_penetration` for the attach rate). On the run of 22 Nov it builds
five pages (Coffee, Mains, …, Soft Drinks & Water) with `--strict`. What it found:

| Gap | Status | How |
| --- | --- | --- |
| D1. The page chip shows "Category: $category (focus)": the filters in force are not bound to the repeat's value, though the queries are | open | A fix in `reports.filters_in_force` (bind the values with the page's params). |
| D2. The attach rate with the other group: a `where` on the category keeps only the category's lines, so penetration by category group shows the own group at 100% | workaround | The order-level flags `has_food` and `has_drink` as dimensions, with the category as a `where`: "With food · With a drink 75.5%" of Mains. An order-level filter ("orders that contain the category") would give the attach rate with any dimension; open. |
| D3. `orders_by_hour` counts on `fct_orders`, which has no category, so the focus would not apply | workaround | The slot overrides the model to `fct_sales_lines` (orders with the category per trading day); the `counts_as_order` filter stays. |
| D4. The top items table repeats the category on every row and cuts the item names | workaround | The slot overrides the dimensions to `adjusted_item_variant` only, and the grid gives it 5 of 12 columns. |
