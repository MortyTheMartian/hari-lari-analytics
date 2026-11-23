# Go-live plan: weekly Moka extraction and report in Google Cloud

Status: draft, 22 Nov 2026. Every task is sized to take roughly one sitting (S under an hour,
M a few hours, L about a day) and ends with a check that tells you it is done.

## Goal

Every Monday at 04:00 Bali time (WITA, UTC+8), a job in Google Cloud does four things. It
downloads the full Moka history through the API, builds the report files, sends them to a new
Telegram reporting group through a new bot, and keeps a copy of the data and reports. The second
goal is to rebuild every chart of the report as a reusable component, so that new reports can be
put together from existing pieces.

## Where we start

- **Extraction works locally.** `extraction/moka/extract.py` pulls the full history in about
  2.5 minutes (around 20 calls and 6 MB). It writes id-rich CSV tables plus the raw JSON. The
  API has no rate limit or cost that matters at our size.
- **The report still reads back-office CSV exports.** `cli.py` → `load.py` / `clean.py` →
  `report_data.py` → `report_pdf.py` + `report_excel.py`. Nothing reads the API tables yet.
- **The token lives in a local file.** `extraction/moka/.token.json` holds an access token
  valid until **11 May 2027** and a refresh token. Its scopes are
  `profile report customer library transaction`.
- **Three things found while preparing this plan.**
  1. **Broken key on the lines table.** `transaction_lines.payment_id` is `0` on every row.
     `Table.add` lets the line's own (empty) `payment_id` field overwrite the parent key, so
     lines can only be joined to receipts through `payment_no`. That key is not unique, because
     refunds reuse it. The same overwrite can hit every child table.
  2. **Wrong time zone label.** API timestamps carry `+07:00`, but the clock time matches the
     back office exactly (receipt at 11:27:53 in both). Keep the clock time and drop the
     offset. Converting to `Asia/Makassar` would move every sale by one hour.
  3. **Refunds have the opposite sign.** A refund arrives as a second payment with the same
     `payment_no`, positive amounts and `parent_payment_id` pointing at the original. The
     back-office CSV shows the same refund as a negative row instead.

## Target architecture

```text
Cloud Scheduler  ── "0 4 * * 1", time zone Asia/Makassar ──►  Cloud Run Job "hari-lari-weekly"
                                                                 │ 1 extract   (Moka API → raw JSON + Parquet)
Secret Manager ◄── read/write Moka token, Telegram token ────────┤ 2 model     (Parquet → DuckDB data models)
                                                                 │ 3 report    (components → PDF, PNG, XLSX)
Cloud Storage  ◄── raw/, tables/, reports/ per run date ─────────┤ 4 deliver   (Telegram sendMediaGroup)
Telegram group ◄─────────────────────────────────────────────────┘ 5 on error: alert message + non-zero exit
```

These are the design choices behind it:

- **Cloud Run Job, not Cloud Functions.** The job runs our own container, has a time limit of
  up to 24 hours (we need about 5 minutes), and runs the same code as on the laptop.
- **DuckDB and Parquet, not BigQuery, for v1.** The data is a few MB. DuckDB runs SQL inside
  the job with no extra service, no cost and no credentials. The components' SQL stays close
  enough to standard SQL to move to BigQuery later if we want Looker Studio or ad-hoc queries
  (optional task 9.10).
- **Full pull every week.** It is cheap and avoids incremental-load bugs. Raw JSON is kept per
  run, so any report can be rebuilt.
- **Region `asia-southeast2` (Jakarta).** It is the closest region to Bali and to Moka's API.
- **Cost.** All of this fits inside the Google Cloud free tier: Cloud Run, three Scheduler jobs,
  six Secret Manager versions, 5 GB of storage and 0.5 GB in Artifact Registry. Expect about
  0 USD a month. A billing budget alert guards against surprises (task 3.4).

## Order of work

```text
Phase 0 decisions ─► 1 harden extraction ─► 2 report reads API data ─► 3 cloud infra ─► 5 orchestration ─► 6 go live
                                                  │                      4 Telegram ──┘
                                                  └─► 7 component framework ─► 8 rebuild charts ─► 8.20 cut over
                                                                                    9 optional new charts
```

Go live first with today's PDF fed from the API (phases 1–6), then swap in the component-based
report once it matches the old one figure for figure (phases 7–8). That way the weekly report
starts flowing while the rebuild happens.

---

## Phase 0: Decisions and accounts

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 0.1 | Decide on a GCP project: a new `hari-lari-analytics` project (recommended) or an existing one. | S | Project ID written down |
| 0.2 | Decide what goes to the group: PDF only, or PDF + Excel lines + a PNG preview of the cover. Recommended: cover PNG as a photo (readable inside Telegram) + PDF + XLSX. | S | Decision noted here |
| 0.3 | Decide who joins the Telegram group. Check that the Excel file is OK for all of them; it has no customer or staff names. | S | Member list |
| 0.4 | Decide where failure alerts go: the reporting group, or a private admin chat with the bot (recommended, so the group only sees reports). | S | Decision noted |
| 0.5 | Confirm the report anchor rule for a Monday run: **anchor = yesterday (Sunday, Bali date)**, not "date of the last sale". A Sunday without sales would otherwise shift every period. | S | Decision noted in METRICS.md |

## Phase 1: Harden the extraction

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 1.1 | Commit the current `extraction/` work on a branch (`.gitignore` already excludes the token and output). | S | Branch pushed, `git status` clean |
| 1.2 | Fix the parent-key overwrite in `Table.add`: parent keys win and child fields that clash get a prefix (`line_payment_id`). | S | `transaction_lines.payment_id` matches `transactions.id` on 100% of rows |
| 1.3 | Add a test for 1.2 using a tiny saved JSON fixture of one payment with two lines, a discount and a modifier. | S | `pytest` green |
| 1.4 | Move the extraction into the package (`src/hari_lari_analytics/extract/`: `client.py`, `extract.py`) and register a CLI command `hari-lari-extract`. | M | `uv run hari-lari-extract --help` works |
| 1.5 | Replace the stdlib HTTP code with `httpx` (or keep stdlib) with explicit timeouts of 120 s and keep the retry and backoff logic and the request log. | S | A full extract runs locally |
| 1.6 | Introduce a `TokenStore` interface with two implementations: local file (today) and Secret Manager (cloud). Writing a refreshed token stores a new secret version. | M | Unit test with a fake store: refresh writes back |
| 1.7 | Refresh the token early: when fewer than 30 days remain (not 1 hour), so a failed refresh shows up weeks before the token expires on 11 May 2027. | S | Test with a fake clock |
| 1.8 | Test a real refresh once by hand. Check whether Moka returns a **new** refresh token (rotation). If it does, writing it back (1.6) is essential. | S | Behaviour noted in this file |
| 1.9 | Make the output location configurable: local folder or `gs://bucket/runs/<run date>/`. Write Parquet next to (or instead of) CSV. | M | Both targets work |
| 1.10 | Parse timestamps as local clock time: drop the `+07:00` offset and store a naive `sale_ts` (see finding 2). | S | Test: `2026-10-28T11:27:53+07:00` → `2026-10-28 11:27:53` |
| 1.11 | Add extraction sanity checks that fail the run: zero payments, no payment in the last 7 days (a warning only, the cafe might be closed), the last run's receipts missing from this run. | S | Checks raise clear errors |
| 1.12 | Write a `manifest.json` per run: row counts per table, first and last sale, number of HTTP calls, duration, token expiry date. | S | File present after a run |

## Phase 2: Make the report read the API data

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 2.1 | Write `load_api.py`, which reads the extracted tables (Parquet) for one run. | S | Returns DataFrames for transactions, lines, line discounts, items, variants, categories |
| 2.2 | Map API lines to today's cleaned line schema (`receipt_nr`, `sale_ts`, `item`, `variant`, `quantity`, `gross_sales`, `discounts`, `refunds`, `net_sales`, `discount_code`, `payment_type`, …) and add the ids: `payment_id`, `item_id`, `item_variant_id`, `category_id`. | M | Same columns as `clean_sales` output plus the ids |
| 2.3 | Handle refunds: lines of a refund payment (`parent_payment_id` set and different from `id`) become negative quantity, and their amount moves into `refunds`. | M | `Gross − Discounts − Refunds = Net` holds on every line |
| 2.4 | Derive the adjusted item and category **by id**: `item_id` → today's item name and `category_id` from `items` (deleted ones included). Fall back to `config/item_aliases.csv` only for deleted items without a live successor. | M | No line ends up "sales fallback" without a note |
| 2.5 | Payment type from `payment_type_label` (BCA, Cash, BCA QR, Gopay …); discount code from `transaction_line_discounts.discount_name`. | S | Payment and discount tables match the CSV-based report |
| 2.6 | **Parity test** against the back-office export from 1 Jan to 31 Oct: same 948 receipts and 1,700 lines. Net sales may differ only by the documented refund of receipt `1O4S7G`. | M | Test passes and the remaining differences are listed |
| 2.7 | Add `--source api|csv` to `hari-lari-report` (default `api`) and `--anchor YYYY-MM-DD` (default: yesterday). | S | Both sources produce a PDF |
| 2.8 | Generate the PDF from API data and compare it page by page with the CSV-based one for the same anchor. | S | Differences explained |
| 2.9 | Render page 1 (cover) also as PNG for the Telegram preview. | S | PNG written next to the PDF |
| 2.10 | Update README and METRICS.md: API as the data source, refund rule, time zone rule, anchor rule. | S | Docs merged |

## Phase 3: Google Cloud infrastructure

All of this can be done with `gcloud` commands kept in `deploy/setup.sh`, so that the setup can
be repeated. Terraform would be overkill for a handful of resources.

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 3.1 | Install and authenticate the `gcloud` CLI in WSL (`gcloud init`, `gcloud auth application-default login`). | S | `gcloud config list` shows the project |
| 3.2 | Create the project (decision 0.1) and link the billing account. | S | Project visible in the console |
| 3.3 | Enable the APIs: `run`, `cloudscheduler`, `secretmanager`, `artifactregistry`, `cloudbuild`, `storage`, `logging`, `monitoring`. | S | `gcloud services list --enabled` |
| 3.4 | Create a budget of 5 USD a month with email alerts at 50% and 100%. | S | Budget listed |
| 3.5 | Create the bucket `gs://<project>-hari-lari` in `asia-southeast2` with uniform access and public access prevention. Add lifecycle rules: raw JSON goes to Nearline after 90 days, nothing is deleted. | S | `gcloud storage buckets describe` |
| 3.6 | Create the service account `hari-lari-job@…` with these roles: Storage Object Admin on the bucket only, and Secret Accessor plus Secret Version Adder on the Moka token secret. | S | Roles listed with `get-iam-policy` |
| 3.7 | Create the secrets: `moka-client-id`, `moka-client-secret`, `moka-oauth-token` (upload today's `.token.json` as version 1), `telegram-bot-token`, `telegram-chat-id`, `telegram-admin-chat-id`. | S | `gcloud secrets list` |
| 3.8 | Create an Artifact Registry Docker repository `hari-lari` in `asia-southeast2`. | S | Repository listed |
| 3.9 | Write a `Dockerfile`: `python:3.13-slim` plus `uv sync --frozen --no-dev`, the Inter fonts are already in the package, and the entry point is `hari-lari-weekly`. | M | `docker run` locally produces the report from a mounted token |
| 3.10 | Build and push the image with Cloud Build (`gcloud builds submit`). | S | Image tag in the registry |
| 3.11 | Create the Cloud Run Job `hari-lari-weekly`: 1 CPU, 1 GiB, 20-minute timeout, 1 retry, the service account from 3.6, secrets as environment variables, `TZ=Asia/Makassar`. | S | `gcloud run jobs execute` succeeds |
| 3.12 | Create the Cloud Scheduler job with schedule `0 4 * * 1`, time zone `Asia/Makassar`, target the Cloud Run Job through an OAuth call with a dedicated invoker service account (`roles/run.invoker`). | S | "Force run" in the console starts the job |
| 3.13 | Add a log-based alert: a Cloud Run Job execution failure emails you. This is a second safety net besides the Telegram alert. | S | Test failure triggers an email |

## Phase 4: Telegram bot and group

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 4.1 | In Telegram, message `@BotFather`, run `/newbot`, name it (e.g. "Hari Lari Reports", username `harilari_reports_bot`), and store the token in Secret Manager only. | S | Token in `telegram-bot-token` |
| 4.2 | Set the bot description, a profile photo (logo) and `/setjoingroups` enabled. Turn joining off again after 4.4 so nobody else can add it to their groups. | S | Bot profile looks right |
| 4.3 | Create the group "Hari Lari · Weekly Report" and add the members from 0.3. | S | Group exists |
| 4.4 | Add the bot to the group. Make it admin only if you want it to pin the latest report (optional). | S | Bot is a member |
| 4.5 | Get the chat id: send a message in the group, call `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` (it starts with `-100` for supergroups). Store it in `telegram-chat-id`. Repeat in a private chat with the bot for `telegram-admin-chat-id`. | S | Both ids stored |
| 4.6 | If the group is later turned into a supergroup (for example by enabling topics), its id changes. Repeat 4.5 then. | — | Note in the runbook |
| 4.7 | Write `deliver/telegram.py`: `send_report(png, pdf, xlsx, caption)` uses `sendPhoto` for the cover, then `sendMediaGroup` with the PDF and the XLSX as documents. Add retries on 429 (respect `retry_after`) and a 50 MB size guard. | M | Test against a test group |
| 4.8 | Write the caption: week number and dates, net sales, orders and AOV with change vs prior week, plus one line about data freshness (last sale timestamp). | S | Caption reviewed |
| 4.9 | Write `send_alert(text)` for the admin chat: run failed at which step, short error, link to the Cloud Run logs. | S | Test alert received |

## Phase 5: The weekly orchestration

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 5.1 | Write the CLI `hari-lari-weekly`: extract → model → report → upload → deliver, each step logged with duration. | M | Runs end to end locally against a test group |
| 5.2 | Add a `--dry-run` flag: everything except the Telegram send, and the outputs go to a `dry-run/` prefix. | S | Dry run leaves the group untouched |
| 5.3 | Make it idempotent: if a report for this anchor was already sent (marker file in the bucket), don't send again unless `--force`. This protects against Cloud Run's retry sending twice. | S | Second run says "already sent" |
| 5.4 | Wrap every step in error handling that sends the admin alert (4.9) and exits non-zero, so Cloud Run marks the run as failed. | S | Forced error → alert + failed execution |
| 5.5 | Log in structured JSON (Cloud Logging picks up `severity`). | S | Logs filterable by step |
| 5.6 | Send a token-expiry reminder to the admin chat once fewer than 45 days remain, and include the expiry date in the manifest. | S | Fake-clock test |

## Phase 6: Go live

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 6.1 | Deploy the image and run the job by hand in dry-run mode in the cloud. | S | Files in the bucket, no message sent |
| 6.2 | Run it for real by hand, sending to the admin chat id instead of the group. | S | Report received privately |
| 6.3 | Open the PDF on a phone inside Telegram and check readability. Adjust the caption or PNG if needed. | S | Looks good on the phone |
| 6.4 | Switch to the group chat id and enable the scheduler. | S | Scheduler "enabled" |
| 6.5 | First live Monday: check at 04:10 WITA that the message arrived and the run took under 10 minutes. | S | Report in the group |
| 6.6 | Write `docs/runbook.md`: how to re-run for a given anchor, how to rotate the Moka token (re-authorise through `auth.py`, upload to Secret Manager), how to add a group member, what each alert means. | M | Runbook merged |
| 6.7 | Keep the laptop pipeline working (`--source csv`) for a month as a fallback, then remove the CSV path. | S | Decision after four good Mondays |

---

## Phase 7: Component framework (the big subgoal)

### The idea

A **component** is one chart or table described by pointers, not by code:

| Input | What it means | Example |
| --- | --- | --- |
| `model` | The data model it reads | `fct_sales_lines` |
| `pattern` | The logic, a parameterised SQL template plus optional post-processing | `trend`, `ranking`, `contributive_aov` |
| `metrics` / `dimensions` | The set from the registry, with some flexibility per component | `[net_sales, orders]` by `sale_week` |
| `filter` | Time window (relative to the anchor) with comparison window, plus dimension filters | `L6W` vs prior 6 weeks; `payment_type = 'Cash'` |
| `facet` | Repeat the same component per value of a dimension (the transpose or pivot) | one trend per `adjusted_category`, top 5 |
| `visual` | Which renderer draws the result, plus display options | `bar_trend` with growth labels |

A **report** is a YAML file that places components into slots of page layouts. Sections such
as "L14D / L6W / L3M" are a facet over periods, so today's report is one section template
repeated three times.

```yaml
# components/category_contributive_aov.yml
id: category_contributive_aov
model: fct_sales_lines
pattern: contributive_aov
dimensions: [adjusted_category]
metrics: [net_sales, items_sold, orders, penetration, category_aov, contributive_aov]
filter: { window: $period, compare: prior }
options: { sort: net_sales desc, total_row: true, keep_prior_only_rows: true }
visual: { type: table, bars: [share], deltas: [penetration, category_aov, contributive_aov] }

# reports/weekly.yml
id: weekly
anchor: yesterday
pages:
  - layout: cover
    slots: { kpis: full_history_kpis, sections: period_summary_cards, notes: data_notes }
  - repeat: { period: [L14D, L6W, L3M] }
    pages:
      - layout: overview
        slots: { kpis: kpi_cards, main: sales_orders_trend, side: key_figures }
      - layout: full
        slots: { main: category_contributive_aov }
      - layout: details
        slots: { left: top_items, right_top: orders_by_hour, right_mid: payment_mix, right_bottom: discount_codes }
```

### Tasks

| ID | Task | Size | Done when |
| --- | --- | --- | --- |
| 7.1 | Write a design note (`docs/components.md`) covering the vocabulary above, the folder layout (`models/`, `patterns/`, `components/`, `reports/`, `visuals/`) and a worked example. Review it before coding. | M | Note agreed |
| 7.2 | Add `duckdb` and `pyyaml` (and `jinja2` for SQL templates) as dependencies. | S | `uv sync` |
| 7.3 | **Data models** as SQL views over the Parquet tables: `stg_*` (one per API table, renamed and typed), `fct_sales_lines` (line grain, all ids, adjusted item and category, `counts_as_order`), `fct_orders` (receipt grain: net sales, items, lines, payment type, discount flag, hour, weekday), `dim_items`, `dim_categories`, `dim_calendar` (date, ISO week, month, weekday, is_trading_day). | L | Views build; row counts match phase 2 |
| 7.4 | Port the cleaning rules into the models: refunds (2.3), fully refunded receipts, Custom Amount category, variant capitalisation. | M | Model output equals `load_api` + `clean` output on the fixture |
| 7.5 | **Metric registry** (`metrics.yml`): name, SQL expression, format (IDR, count, %), `up_is_good`, additivity (orders are **not** additive), and the grain it must be computed at. | M | All METRICS.md metrics defined |
| 7.6 | **Dimension registry** (`dimensions.yml`): name, column or expression, sort order, display label. | S | All METRICS.md dimensions defined |
| 7.7 | **Window resolver**: `L14D`, `L6W`, `L3M`, `full`, `WTD`, `MTD`, `last_n_weeks(n)` plus `compare: prior` and `compare: same_period_last_year`. Reuse the logic and tests of `periods.py`. | M | Existing period tests pass through the resolver |
| 7.8 | **Pattern engine**: render a Jinja SQL template with the metrics, dimensions and filters, run it in DuckDB, return a DataFrame plus metadata (formats, comparison columns). | M | One pattern runs end to end |
| 7.9 | Patterns, one task each (S–M): `kpi_summary` (current, prior, change), `trend` (buckets incl. empty ones, prior aligned, growth), `sparkline` (7-day blocks counted back), `ranking` (top N with share), `share_breakdown` (with "Other" grouping), `contributive_aov` (incl. total row and prior-only rows), `distribution` (per hour, average per trading day). | 7 × S/M | Each pattern has a unit test against a hand-computed fixture |
| 7.10 | **Facet engine**: run a component once per value of a dimension (all, top N by a metric, or an explicit list), and once per period for section templates. | M | Trend faceted by category renders 5 small charts |
| 7.11 | **Component loader**: read a component YAML, validate it against a schema (pydantic) so a typo fails loudly, and resolve the pointers. | M | Invalid YAML gives a readable error |
| 7.12 | **Visual library**: move `Palette`, `_card`, `_chips`, `_table`, `_bar_column`, `_sparkline` out of `report_pdf.py` into `visuals/base.py`. A visual takes `(fig, rect, data, options)` and does no data work. | M | Old report still renders identically |
| 7.13 | **Layout engine**: named page layouts (`cover`, `overview`, `full`, `details`, `grid(n×m)`) that map slot names to rectangles. | M | A test report with a 2×2 grid renders |
| 7.14 | **Report builder**: read the report YAML, expand repeats and facets, run the components, place the visuals, write the PDF and per-page PNGs. | M | `hari-lari-report --report reports/weekly.yml` works |
| 7.15 | Cache component results per run, so a facet or a repeated component doesn't re-query. Optional, since DuckDB on a few MB is fast anyway. | S | — |

## Phase 8: Rebuild every current chart as a component

Each row is one task (S–M): write the component YAML, any pattern options it needs, its visual,
and a **parity test** that compares its data with today's `report_data.Section` for the same
anchor.

| ID | Current piece (where it lives today) | Model | Pattern | Visual | Facet |
| --- | --- | --- | --- | --- | --- |
| 8.1 | Full-history KPI cards on the cover (`_cover`) | fct_sales_lines | kpi_summary, window `full` | `kpi_card` ×4 | — |
| 8.2 | Gross / discounts / refunds line on the cover | fct_sales_lines | kpi_summary | `text_line` | — |
| 8.3 | Period summary cards: net sales and orders with change, sparkline (`_cover`) | fct_sales_lines | kpi_summary + sparkline | `summary_card` | period |
| 8.4 | Data notes card (`_cover`) | run manifest + model checks | `notes` (not SQL) | `notes_card` | — |
| 8.5 | KPI cards with change, prior and sparkline (`_kpi_cards`) | fct_sales_lines | kpi_summary + sparkline | `kpi_card` | metric (4) |
| 8.6 | Net sales & orders trend with prior ticks and growth labels (`_trend`) | fct_sales_lines | trend | `bar_trend` | period |
| 8.7 | Key figures table, 7 KPIs plus first and last sale (`_key_figures`) | fct_sales_lines | kpi_summary | `kpi_table` | — |
| 8.8 | Categories and contributive AOV table (`_categories`) | fct_sales_lines | contributive_aov | `table` with bars and deltas | — |
| 8.9 | Order penetration dot plot (`_penetration_chart`, built but not used today) | fct_sales_lines | contributive_aov | `dot_plot` | — |
| 8.10 | Top 15 items (`_items`) | fct_sales_lines | ranking by item-variant | `table` with bars | — |
| 8.11 | Orders by hour, average per trading day, top 3 highlighted (`_hours`) | fct_orders | distribution | `hour_bars` | — |
| 8.12 | Payment types stacked bar with legend and "Other" (`_payments`) | fct_orders | share_breakdown | `stacked_share_bar` | — |
| 8.13 | Discounts by code (`_discounts`) | fct_sales_lines | share_breakdown | `table` | — |
| 8.14 | Page header chips and footer (`_page`, `_section_page`) | report context | — | layout chrome | — |
| 8.15 | Empty-period page (`_empty`) | — | — | layout fallback | — |
| 8.16 | Excel export of the cleaned lines (`report_excel.py`) | fct_sales_lines | `export` | XLSX writer | — |

| ID | Wrap-up task | Size | Done when |
| --- | --- | --- | --- |
| 8.17 | Write `reports/weekly.yml`, which reproduces today's PDF from the components. | S | PDF builds |
| 8.18 | Visual diff: render old and new PDF to PNG and compare page by page (`pdftoppm` + pixel diff). Accept only intentional differences. | M | Diff report reviewed |
| 8.19 | Write a second report from existing components only (for example `reports/category_deep_dive.yml`, which facets the trend and the penetration by category), to prove the reuse works. | S | Built without new Python code |
| 8.20 | Cut over: `hari-lari-weekly` builds `reports/weekly.yml`, then delete `report_data.py` and the old page methods. | S | Next Monday's report comes from components |
| 8.21 | Document how to add a component and a report (README section). | S | Merged |

## Phase 9: Optional new components (ideas from exploring the API data)

These follow the KPI tree: **Sales = Orders × AOV**, and **AOV = Σ contributive AOV =
Σ order penetration × category AOV**. The customer branch is left out. Each idea notes what
the data showed.

| ID | Component | Why it is interesting | Pattern / facet |
| --- | --- | --- | --- |
| 9.1 | **Orders per trading day by weekday** | It stands in for order frequency without customer tracking. Friday has 427 receipts against 118 on Monday (full history), so weekday mix drives weekly swings. | distribution by weekday, compare prior |
| 9.2 | **Weekday × hour heatmap** of orders | Shows staffing and opening hours. Sales cluster at 07:00–14:00 with a small evening bump at 17:00–19:00. | new `heatmap` visual |
| 9.3 | **AOV bridge: items per order × average item price** | A second split of AOV next to the category split. It averages 2 items per order; 54% of orders have only one line. | kpi_summary with derived metrics |
| 9.4 | **Basket size distribution** (lines per order: 1, 2, 3, 4+) with trend | Upselling target: moving single-item orders to two. | distribution on fct_orders |
| 9.5 | **Penetration and category AOV trend per category** | Makes the bottom of the KPI tree visible over time, and is the showcase for faceting. | trend, facet by category (top 6) |
| 9.6 | **Food + drink attach rate** | Share of orders with a drink that also have food, and the reverse. A direct lever on AOV. | new `attach_rate` pattern over category groups |
| 9.7 | **Discount dependence over time** | `FOUNDER25` is on 456 lines (about 17% of all lines) and `EVENTHOST100` on 59. Shows discount rate per week and per code, and net sales with and without discounted orders. | trend + share_breakdown, facet by code |
| 9.8 | **Item Pareto (ABC)** | Which items make 80% of net sales; candidates to drop from the menu. | ranking with cumulative share |
| 9.9 | **New, rising and fading items** | Change in items sold vs prior period per item, so menu changes show up. | ranking on the delta |
| 9.10 | **BigQuery + Looker Studio mirror** (infrastructure, not a chart) | Ad-hoc exploration in the browser. Load the weekly Parquet into BigQuery; free at this size. | — |
| 9.11 | **Gross margin by item and category** | Needs COGS: only 182 of 2,704 lines carry a `cogs` value today. Precondition: fill in COGS per variant in Moka. | contributive pattern on margin |
| 9.12 | **Menu engineering matrix** (popularity × margin) | Classic Stars / Plowhorses / Puzzles / Dogs view. Depends on 9.11. | new `scatter_quadrant` visual |
| 9.13 | **Unpriced add-ons from line notes** | 9% of lines have a note, and "oat" or "oat milk" appears about 70 times, while only 10 lines use real modifiers. Oat milk as a priced modifier would capture that revenue. A one-off analysis first, a component later. | text match on `note` |
| 9.14 | **Payment mix trend plus estimated card fees** | BCA EDC carries 76% of receipts. With the MDR rate entered in config, show the estimated fees per week. | share_breakdown trend |
| 9.15 | **Refund and void log** | A small table of every refund in the period (receipt, item, amount, reason) for control. Rare today, so a list beats a chart. | `list` pattern |
| 9.16 | **Category hygiene check** (data quality, not a chart) | Lines carry 19 categories, including near-duplicates (`Focaccia ` vs `Snacks (Focaccia)`, `Soft Drinks` vs `Soft Drinks & Water`) and 268 `Uncategorized` lines. Clean them up in Moka, then mapping by id (2.4) makes the history consistent. | data-notes card |

## Risks and how the plan handles them

| Risk | Handling |
| --- | --- |
| The Moka token expires (11 May 2027) or refresh fails | Early refresh (1.7), write-back (1.6), reminder at 45 days (5.6), re-auth steps in the runbook (6.6) |
| Moka API is slow (1–17 s per call) or briefly down | Retries with backoff, 20-minute job timeout, one Cloud Run retry, idempotent send (5.3) |
| A refresh token rotates and the old one is lost | New Secret Manager version written before the job continues; old versions are kept |
| Report sent twice | Sent-marker per anchor (5.3) |
| Wrong numbers after the switch to the API | Parity tests (2.6, 8.x) and visual diff (8.18) before each cutover |
| Telegram chat id changes on supergroup upgrade | Runbook note (4.6); the send fails loudly and alerts the admin chat |
| Personal data in the files | Names stay stripped in the Excel export; raw JSON stays in a private bucket |
