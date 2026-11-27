# Decisions

Every design decision of the project, numbered in the order it was taken. A decision stays in
this file when a later one replaces it; the status says which one, and from when. The designs
they belong to are in [components.md](components.md) (the reporting framework, D1 to D19) and
[architecture.md](architecture.md) (the platform, from D20).

The gaps `D1` to `D4` in [framework-gaps.md](framework-gaps.md) (the category deep dive) are a
different series.

## The reporting framework (D1 to D19)

D1 and D2 were agreed on 24 Nov 2026 with the first version of the design note. D3 to D10 were
agreed on the same day after the revision (plan task 7.1b), each as proposed. D11 to D14 were
agreed on 25 Nov 2026 (plan task 7A.9), D15 to D19 on the same day (plan tasks 8.0 and 8.3).

| # | Question | Decision | Status |
| --- | --- | --- | --- |
| D1 | Where the definitions live | agreed: inside the package, now the modules of [architecture.md](architecture.md) (`semantic/`, `analysis/`, `presentation/`, `warehouse/`) | in force (split in t7, 27 Nov 2026) |
| D2 | Data source of the component report | agreed: sales from API runs only; the hand-maintained `config/*.csv` files (aliases, category groups, later margins) stay and are loaded as `seed_*` tables | in force; the config leaves git in phase B (t11) |
| D3 | Layout definitions | agreed: a 12-column grid in the report YAML, with a minimum size per visual and an overflow test (replaces the rectangles in `layouts.yml`) | in force |
| D4 | Where Python lives | agreed: one query compiler; Python only for transforms, visuals, insight rule types and `@query` sources; none in components or reports (replaces the Jinja SQL pattern per chart with an optional `post` step) | in force |
| D5 | Explaining changes | agreed: a KPI tree in `kpi_tree.yml` and a `decompose` transform with log-mean (LMDI) attribution | in force |
| D6 | Outputs | agreed: PDF, PNG, Telegram text and XLSX from one report; the Telegram caption becomes a report output and replaces `deliver/caption.py` at the cut-over | in force |
| D7 | Sample size | agreed: a default rule of 30 orders per row or period, overridable per metric; rows below it are greyed out with their `n`, and insights skip them | in force |
| D8 | Build or adopt | agreed: own framework, with dbt's model layers and MetricFlow's metric kinds as concepts only; no dbt installation; models stay plain SQL run by DuckDB in-process | for the warehouse, replaced by D21 if the spike says go (D24); the rest stays our own |
| D9 | Acceptance of the port | agreed: parity of each ported component's numbers with `report_data.Section`; no pixel diff, so the drawing may improve | done (the port closed in 9.7b) |
| D10 | Joining category groups | agreed: by adjusted category name; `category_id` in `config/category_group.csv` is informational, because Moka reuses category ids | in force |
| D11 | Meaning of a report or page filter | agreed: two keys, `where` and `focus`, with their query meaning; a filter replaces the component's own on the same dimension, and a slot can opt out | in force |
| D12 | Running several scheduled reports | agreed: one Cloud Run job for all reports, one Cloud Scheduler job per report that passes `run --report <id>`; the release path still promotes one live job and its dry-run twin | replaced by D22 at the cut-over of phase B (t17) |
| D13 | Who keeps the scheduler jobs in line with the YAML | agreed: the release workflow, after the dry run and promotion; the release service account gets `cloudscheduler.admin` on the project | replaced by D22 at the cut-over of phase B (t17): the release manages the one tick job |
| D14 | Telegram chats a report can deliver to | agreed: named targets in `project.yml`, each mapped to the Secret Manager secret of its chat id; `admin` is required; a new chat (managers, a stock-alert group) needs no code | in force until audiences replace targets in phase C (t20) |
| D15 | Visual contract | agreed: options say what to show, never how; colours are theme tokens and text sizes text roles; one formatting module reads `project.yml`; a neutral change (`up_is_good: null`) is drawn in the text colour | in force |
| D16 | Where display strings are made | agreed: one presentation step (`present.py`) turns a result into display cells (value, text, tone, small-sample flag, n); the matplotlib visuals and the JSON bundle both use it | in force |
| D17 | Small samples in a visual | agreed for now: the value and its change in grey with `n=…`, the change never coloured; to revisit after reading the first reports | in force; to revisit after the owners' feedback (t44) |
| D18 | Calendar events on time visuals | agreed: the compiler puts the events of both windows in `meta.events`; visuals and the bundle read them from the result | in force |
| D19 | The `record` insight | agreed: only when the latest bucket is the best of the series (the lowest for a metric where down is good); "since opening" only when the series starts at the first sale | in force |

## The platform (from D20)

D20 to D23 were agreed on 26 Nov 2026 with the platform plan, which continues the go-live plan.
D24 is reserved for the dbt spike. D25 was agreed on 27 Nov 2026 with
[architecture.md](architecture.md) (plan task t3).

- **D20. Break off.** The go-live plan closes after 9.8 and is archived; its open items are
  carried over into the platform plan.
- **D21. dbt-core with dbt-duckdb for the warehouse, adopted when the second source lands**
  (spike t26, go or no-go D24). The semantic layer, the KPI tree, the compiler, transforms,
  insights and the presentation stay our own: that is the part no tool does better for this
  café. D8 rejected dbt for cost; dbt-core is free and runs in-process, so the trade-off is one
  dependency against snapshots of history, sources and freshness, multi-source staging, lineage,
  and a manifest that Lightdash, Evidence and MetricFlow read.
- **D22. One daily tick.** One Cloud Scheduler job starts one Cloud Run job a day: extract the
  sources that are due, build the warehouse, then run the reports that are due. A report's
  `schedule` says on which days it is due, the tick says when.
- **D23. Jibble is the first new source**, because it unlocks labour cost % and sales per labour
  hour, which no Moka data can.
- **D25. Insight rules, facets and the semantic manifest belong to `presentation`, not
  `analysis`.** Insight sentences are made of display strings (D16), a facet repeats a component
  inside a slot, and the manifest lists components and reports; in `analysis` each would import
  upwards. `analysis` is the layer of numbers only: the compiler, windows, results, transforms
  (with `decompose`) and the `@query` sources.

### Phase B (D26 to D31)

Agreed by the owner on 27 Nov 2026 (plan task t10); the design is in
[architecture.md](architecture.md), sections "Storage layout" to "The run ledger".

- **D26. Storage layout and the landing manifest as designed.** Every folder is named by its
  run date, never by the last day of its data; one landing run per source and date, whose
  `manifest.json` is written last and is the commit; tables are `events` (the window) or
  `snapshot` (the state at the run). Retention: landing tables, manifests, reports and the
  ledger are kept; raw JSON goes after 90 days, `warehouse/<date>/` after 30 days, `dry-run/`
  after 14 days.
- **D27. The tick at 04:00 WITA, with a retry tick at 06:00** from the same scheduler job
  (`0 4,6 * * *`). Every step is idempotent on the ledger, so the 06:00 tick does only what the
  04:00 tick left undone.
- **D28. A report is due when the day fields of its cron match the date** in the project time
  zone. Hour and minute must be single numbers and are not read; the per-report `timezone` key
  goes. No catch-up: a report not sent on its due date is sent by hand
  (`tick --date <due date> --only <report>`).
- **D29. A report's sources are derived** from the models its components read, down to the
  `raw_` tables and `sources.yml`; shown by `hari-lari-report list` and pinned by a test.
- **D30. Failure policy.** A failed source does not land and alerts the admin once per tick;
  reports that need it are skipped, the others run; no fallback to an older landing run. A
  failed data test of severity error stops every report and keeps the previous `latest.json`.
  A report still not sent after the last tick of its due date is announced to its own chats as
  a plain message (the admin's alert has the error and the command). A good day is silent.
- **D31. One ledger record per step**; a report's record holds its deliveries, each with its own
  status, so a re-run sends only to the targets that missed it. Targets by name, never chat ids.

### Open

- **D24.** dbt-duckdb go or no-go, decided by the owner after the spike (t26).
- The choices of phase C (t20) are recorded here as they are answered.
