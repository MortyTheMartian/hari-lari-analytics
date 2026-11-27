---
name: Hari Lari analytics platform
overview: "Turn the go-live repository into a modular BI platform for Hari Lari at under 10 EUR a month (expected: under 1 EUR). First draw module boundaries without changing any output, then put a stored warehouse between the data and the reports: one daily run extracts every source into a landing zone, builds the warehouse and only then runs the reports that are due, with a run ledger instead of sent-markers. Then decouple delivery (audiences and channels instead of Telegram and Drive in the report YAML), prove the structure on a second source (Jibble attendance, with dbt-duckdb adopted at that point) for labour cost and sales per labour hour, add margins and expenses for prime cost and a profit estimate, and finally a read-only frontend over the report bundles and the publishing of the code. Continues docs/archive/hari_lari_analytics_go-live_47a99005.plan.md, closed after phase 9.8."
todos:
- id: t1-monday
  content: "[owner] First live Monday, 30 Nov 2026 (old 6.5): at 04:10 WITA check that the weekly report with the 9.8 insights arrived in the group, and read cover, caption and PDF on the phone. Optional before then: one run to the admin chat (runbook, 'Running it by hand', with --to admin). If it failed or looks wrong: deploy/release.sh rollback returns the live job to b6fe84dac12c (the report without insights), then send by hand. Report back: 'Monday ok' or what was wrong."
  llm: "none (owner only)"
  status: completed
- id: t2-token-purge
  content: "[agent] Old 11.1, urgent: delete refs/original/, expire the reflog, git gc --prune=now, and confirm git log --all -- extraction/moka/.token.json.bak-2026-11-22 returns nothing. Then test whether the access token from that backup still works against the Moka API; if it does, the owner re-authorises (runbook, 'Re-authorising the Moka token') and the test is repeated. Local check: the git log command prints nothing and the token test result is written into this todo. Result 27 Nov 2026: purged (git log prints nothing, blob and commit gone; GitHub never had the commit); backup access token returns 401 'The access token was revoked', current token 200, no re-authorisation needed."
  llm: claude-opus-5-5-high
  status: completed
- id: t3-architecture
  content: "[agent] Write docs/architecture.md from the 'Target architecture' section of this plan: modules with their responsibility and allowed dependencies, the engine and content split, the storage layout, the run ledger, the warehouse principle (rebuildable from landing plus config), the end-state warehouse model and the cost budget. Move the decisions D1 to D19 out of docs/components.md ('Decisions') into docs/decisions.md, leave a link behind, and add D20 to D23 from this plan's 'Decisions'. Local check: every relative link in the three files resolves (a small script or rg), and docs/components.md no longer holds the decisions table. Result 27 Nov 2026: done; owner decided D25 (insight rules, facets and the semantic manifest in presentation)."
  llm: claude-opus-5-5-high
  status: completed
- id: t4-housekeeping
  content: "[agent] Delete the merged local branches (framework-core, go-live-extraction, initial-version, presentation, prove-port, remove-old-report, report-contributive-aov-pr, sales-report-redesign, chart-insights) and, after the owner confirms, the merged remote branches. Add paths-ignore (docs/**, *.md) to the push trigger of .github/workflows/release.yml, so a docs-only merge does not rebuild and promote the image. [owner] Find out why git push over the github-personal SSH alias is refused (publickey) in agent sessions; until then agents push over HTTPS with the gh login (git -c 'credential.helper=!gh auth git-credential' push https://github.com/MortyTheMartian/hari-lari-analytics.git <branch>). Result 27 Nov 2026: deleted local branches framework-core, go-live-extraction, initial-version, presentation, prove-port, remove-old-report, report-contributive-aov-pr, sales-report-redesign, chart-insights; updated `.github/workflows/release.yml` push trigger to ignore docs/** and *.md. Remote deletion done for origin/framework-core, origin/initial-version, origin/presentation, origin/sales-report-redesign. Local check note: `git branch -a` may still show other remote-tracking branches; those were not part of the merged-remote deletion set."
  llm: gpt-5.4-nano-medium
  status: completed
- id: t5-golden
  content: "[agent] Freeze what the pandas pipeline still guards: generate once, from clean.py, dimensions.py and load_api.py, the expected fct_sales_lines (all columns) and the period numbers that tests/test_port.py, test_tracer.py, test_models.py, test_windows.py and test_transforms.py compare with, as CSV files under tests/golden/ for the fixture; switch those tests to the golden files; drop the real-run comparisons with pandas (PORT_RUN), keeping the real-run checks that do not need pandas. Local check: the full suite passes, and a test asserts that none of hari_lari_analytics.clean, .dimensions, .metrics or .load_api's pandas mapping is imported after the suite ran. Start after t1-monday. Result 27 Nov 2026: tests/golden/ holds 15 line cases (the fixture, a second anchor, two first weekdays, six variant and four item-resolution cases, two discount codes), periods.csv and three metrics files, written by tests/golden/make_golden.py (delete in t6); owner widened the scope: test_clean.py and test_metrics.py deleted, test_load_api.py keeps only run loading, registry, project and category-group tests moved off pandas, load_api imports clean only inside the mapping; tests/test_zz_pandas_pipeline_unused.py runs last; 1064 passed."
  llm: claude-opus-5-5-high
  status: completed
- id: t6-retire-pandas
  content: "[agent] Delete the pandas pipeline (clean.py, dimensions.py, metrics.py, report_excel.py, the pandas mapping in load_api.py, the CSV source and tests/fixtures/item_aliases_full_2026-11.csv, absorbing old 6.7); move the seed loaders (load.py) and load_run into the warehouse code, the lines-export columns into the lines_export query, and periods.py into windows.py. Update README and METRICS.md. Local check: ruff, all validate, schema and manifest checks and the suite pass; hari-lari-report build --report weekly --anchor 2026-11-24 --run data/extract/2026-11-25 writes a bundle.json equal to the one before the change (a diff of the two files, ignoring the build timestamp). Blocked by t5-golden. Result 27 Nov 2026: done; run reader and seed loaders in reporting/inputs.py (load_run reads what sources.yml lists, required tables fail in open_run), periods in windows.py, export columns in queries/lines_export.py, sheet writer in outputs/xlsx.py; bundle.json and caption identical (only the build timestamp differs). Owner added: the Excel lines sort by their key, so a workbook is identical between builds (tie order varied before), and a warn check for refunds whose lines differ from Moka's refund_amount (only the pandas mapping had it). 1055 passed."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t7-modules
  content: "[agent] Move the code into the module layout of 'Target architecture' (core, sources/moka, warehouse, semantic, analysis, presentation, orchestration, distribution) without changing behaviour: console scripts keep their names, YAML files keep their $schema lines (paths updated), the Dockerfile, the Cursor rule's globs, docs/components.md paths and README follow. delivery.py moves to orchestration as it is; its split comes in phase C. Local check: as t6 (bundle.json identical) plus the PDF pages pixel-identical (pdftoppm at 100 dpi and a byte compare of the PNGs), ruff, validate, schema and manifest checks, and the suite. Blocked by t6-retire-pandas, t3-architecture. Result 27 Nov 2026: done; code is in core, sources/moka, warehouse, semantic, analysis, presentation, orchestration and distribution; bundle.json identical and the 10 PDF pages pixel-identical at 100 dpi; 1055 passed."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t8-import-contract
  content: "[agent] Add import-linter (dev dependency) with a layers contract in pyproject.toml matching docs/architecture.md, plus an independence contract (distribution and sources import only core), and a CI step lint-imports. Local check: lint-imports passes; a deliberate upward import in a scratch file fails with the contract named; CI green on the pull request. Blocked by t7-modules. Result 27 Nov 2026: done; lint-imports keeps the layer, only-core and independence contracts; a warehouse import of presentation fails the Module layers contract."
  llm: gpt-5.6-terra-medium
  status: completed
- id: t9-release-a
  content: "[agent] Merge phase A on a Tuesday to Thursday (never between Saturday 18:00 and Monday 05:00 WITA), watch the release workflow to the promotion, and compare the next Monday's bundle.json in gs://hari-lari-data/reports/weekly/<anchor>/ with a local build on the same run. [owner] Confirm the Monday report looked as before. Blocked by t8-import-contract. Result 27 Nov 2026: merged as bd35ece; dry run succeeded; live job promoted ff86fcaac81d -> bd35ece9d9f7; the weekly scheduler is still Monday 04:00 WITA. 27 Nov: a send of the released report to the owner's Telegram worked. Still open: Monday's bundle compare, and the owner confirms the report looked as before. Note 27 Nov: phase B (t11 to t18) is planned for today in one release, so Monday 30 Nov's run is the first tick on an image with phases 0, A and B; the bundle compare then covers A and B together (the bundle from the tick against a local build on the same landing run), and Monday's confirmation closes t9 and t17 at once."
  llm: gpt-5.6-terra-medium
  status: pending
- id: t10-design-b
  content: "[agent] Design note for phase B in docs/architecture.md, with the open choices put to the owner: the storage layout (landing/<source>/<date>/, warehouse/<date>/, reports/, runs/, config/), the manifest every source writes, the ledger record (one object per run step, fields), when a report is due (its cron evaluated for the tick's date in the project time zone), the tick time (proposed 04:00 WITA, so the weekly report keeps its hour), which sources a report needs (derived from its components' models), and the failure policy (proposed: a failed source alerts once; reports that need it are not sent; reports that do not need it run). Record the owner's answers as decisions. Local check: the owner has answered every open choice. Blocked by t9-release-a. Result 27 Nov 2026: design in docs/architecture.md, owner answered all eleven choices, recorded as D26 to D31; all recommendations taken except catch-up: none, a report not sent by the last tick of its due date is announced to its own chats and sent by hand."
  llm: claude-opus-5-5-high
  status: completed
- id: t11-private-config
  content: "[agent] Old 11.2: the business config leaves git and the image. CONFIG_DIR is a local folder (default config/, now untracked) or gs://hari-lari-data/config/; the Dockerfile stops copying config/; a missing required file fails the run with the admin alert; config/examples/ holds the same columns with demo values. calendar_events.csv stays public if it holds only public holidays and closures. [owner] Upload the real files once (gcloud storage cp config/*.csv gs://hari-lari-data/config/). Local check: a cloud dry run (deploy/release.sh dryrun) builds the weekly report with the config from the bucket. Blocked by t10-design-b. Result 27 Nov 2026: done in 162b14c; config/*.csv untracked with config/examples/, calendar_events.csv private too, the CSVs in gs://hari-lari-data/config, cloud dry run of the branch image green."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t12-landing
  content: "[agent] The Moka extraction writes to landing/moka/<date>/ with the manifest of t10 (source, run_at, tables with row counts, the schema of each table); a one-off script copies the existing raw/<date>/ runs into landing/moka/ so the history of catalogue and stock snapshots is kept; the bucket lifecycle deletes raw JSON after 90 days and dry-run/ after 14 days, and keeps Parquet (D26). Local check: hari-lari-extract --out data/landing writes a run that the new manifest schema validates; the copy script lists every run it copied with its row counts. Blocked by t10-design-b. Result 27 Nov 2026: done in 02be422; Moka lands as landing/moka/<date>/ with the manifest and its JSON Schema, raw/ copied into landing/moka/, lifecycle rules set (raw/ to Nearline at 90 days, landing .json.gz deleted at 90, warehouse/<date> at 30, dry-run/ at 14)."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t13-warehouse-build
  content: "[agent] New command hari-lari-warehouse build [--date] [--dry-run]: reads the latest landing run per source and the config, builds the models, runs the data checks, and writes warehouse/<date>/warehouse.duckdb, warehouse/<date>/marts/<model>.parquet (exposed models only, never line notes or personal data, old 12.2) and warehouse/latest.json; the bucket lifecycle deletes warehouse/<date>/ after 30 days (D26). The warehouse holds nothing that cannot be rebuilt from landing and config. Local check: a build on data/landing writes the three outputs; opening the DuckDB file and the marts gives the same row counts and net sales as today's in-memory build on the same run. Blocked by t12-landing, t11-private-config. Result 27 Nov 2026: done in 49f2baf; the build of 27 Nov takes about 2 s and writes 9.4 MB; file, marts and in-memory build agree on the rows of all 6 exposed models and net sales 113,299,125; private columns (note) stay out of the marts."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t14-reports-read-warehouse
  content: "[agent] Report runs open the warehouse file read-only instead of extracting and rebuilding: --warehouse <date|path> replaces --run; the notes source and the extraction time come from the warehouse's run context; hari-lari-component run reads the warehouse too. Local check: hari-lari-report build --report weekly on the warehouse of t13 writes a bundle.json equal to the build from the extraction run; the report step gets faster (time both). Blocked by t13-warehouse-build. Result 27 Nov 2026: done in c57f1ed; the weekly bundle from the warehouse equals the one from the run except the footer's build time, caption identical; the model step takes 0.05 s instead of 0.65 s."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t15-ledger
  content: "[agent] The run ledger: every step of a run (extract per source, warehouse, report, delivery) writes one record runs/<date>/<run id>-<step>.json with status, timings, inputs (landing dates, warehouse date, image tag), outputs and deliveries (target names and message ids, Drive links; fields as in docs/architecture.md, D31). 'Already sent' becomes a ledger lookup (the old sent-markers are still honoured); reports/index.json is regenerated after each report run (old 12.1); hari-lari-runs list [--report] [--since] prints the history. Local check: two local runs, the second without --force, show 'already sent' from the ledger; index.json lists both anchors. Blocked by t14-reports-read-warehouse. Result 27 Nov 2026: done in 42e02cf; records under runs/<date>/, already-sent read from the ledger (old markers still read and written until t17 is confirmed), reports/index.json after each report run, hari-lari-runs list; the second run says already sent."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t16-tick
  content: "[agent] New command hari-lari-pipeline tick [--date] [--dry-run] [--only <report>]: extracts the sources that are due, builds the warehouse, runs every report that is due as decided in t10 (D28 to D30: one failing report does not stop the others; the report loader refuses an hour or minute that is not a single number and the timezone key; after the last tick of the day a report still not sent is announced to its own chats), writes the ledger and sends ops alerts. deploy/: one Cloud Scheduler job hari-lari-tick at the tick times of project.yml (04:00 and the 06:00 retry, D27); the release dry run becomes tick --dry-run; deploy/release.sh schedules manages only the tick and deletes the hari-lari-report-* jobs. Local check: tick --dry-run --date 2026-11-30 (a Monday) builds weekly and skips nothing else; tick --dry-run --date 2026-12-01 builds no weekly; tests for due rules and the failure policy with a fake source that fails. Blocked by t15-ledger. Result 27 Nov 2026: done in 469a638; local dry tick for 30 Nov builds weekly only (2 min 23 s with the extraction), for 1 Dec nothing (3 s); cloud dry run of 469a63856494 on hari-lari-weekly-dryrun green; the scheduler is unchanged until t17 merges (hari-lari-report-weekly still the Monday job). Not done: D29's derivation of a report's sources (every report needs every source while moka is the only one)."
  llm: claude-sonnet-5-5-high
  status: completed
- id: t17-cutover-b
  content: "[agent] Release phase B midweek: cloud dry run, one tick to the admin chat (--to admin), then switch from hari-lari-report-weekly to hari-lari-tick; update docs/runbook.md for the new flow (absorbs old 6.6: re-running a day, re-authorising tokens, what each alert means). [owner] Read the admin-chat report on the phone and confirm the switch. Local check: gcloud scheduler jobs list shows hari-lari-tick only (plus the expense tracker's own job); the first daily ledger records appear. Planned 27 Nov 2026 (Friday, with t11 to t18 the same day, see 'Rules for this plan'): this release is the one image with phases 0, A and B on Google Cloud; record the promoted image tag in this todo and check that the `hari-lari-tick` job runs that tag. Monday 30 Nov, 04:00 WITA, is the live test of it all: the owner reads the report on the phone at 04:10 and reports 'Monday ok' or what was wrong, and `deploy/release.sh rollback` returns the live job to the previous image. Blocked by t16-tick. Result 27 Nov 2026 (Friday, 20:26 to 20:40 WITA): released with pull request 12, promoted image `df650ed5e4d3` (previous `bd35ece9d9f7`), run by `hari-lari-weekly` and `hari-lari-weekly-dryrun`; `gcloud scheduler jobs list` shows `hari-lari-tick` (`0 4,6 * * *`, enabled) and `expense-bot-tick` only, the release deleted `hari-lari-report-weekly`; `tick --to admin --all-scheduled` on the live job (execution hari-lari-weekly-t78sz, 154 s) sent the report to the admin chat and wrote the first ledger records (run 203436-7dab); owner read it on the phone: 'looked fantastic'; docs/runbook.md rewritten for the tick. Stays open until Monday 30 Nov: 'Monday ok' closes it together with t9. Until Saturday 18:00 the fallback is `deploy/release.sh rollback`, which goes back to `bd35ece9d9f7` (not to the `b6fe84dac12c` named in t1) and restores `hari-lari-report-weekly`."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t18-cost-hygiene
  content: "[agent] After a Moka token refresh, disable the previous Secret Manager version (active versions are billed beyond the free 6); check the Artifact Registry and bucket lifecycle rules; after the first month on the daily tick, read the bill and write the actual monthly cost into 'Cost budget'. Local check: gcloud secrets versions list moka-oauth-token shows one enabled version after a forced refresh in a test project or with a fake client. Planned 27 Nov 2026: done the same day as t11 to t17 and included in the same release; only the bill reading waits for the first month on the tick (about 1 Jan 2027) and is then written into 'Cost budget'. Blocked by t17-cutover-b. Result 27 Nov 2026 (checked, not finished): the bucket lifecycle is the four rules of t12 (raw/ to Nearline at 90 days, landing .json.gz deleted at 90, warehouse/2* at 30, dry-run/ at 14), `moka-oauth-token` has one enabled version, Cloud Scheduler uses 2 of the 3 free jobs; Artifact Registry holds 5.5 GB against 0.5 GB free, about 0.5 USD a month and not 0 (its policy keeps 20 versions and deletes after 60 days; 'Cost budget' corrected). Found: Google bills disabled secret versions as active, only destroyed ones are free, so 'disable the previous version' saves nothing; pull request 13 (disable, with fake-client tests) is a held draft, and the owner chooses: destroy instead (irreversible, saves 0.06 USD a month per old version, an old Moka token is revoked anyway) or close it. Still open: that choice, and the bill after the first month on the tick (about 1 Jan 2027). The overview still says 'under 1 EUR'; the corrected budget is about 1 EUR."
  llm: gpt-5.6-terra-medium
  status: pending
- id: t19-stock-daily
  content: "[agent] First payoff of the daily data: stock_alert gets a daily schedule with deliver.when rows, so it is sent only when something is at or below its alert quantity, to the target the owner names (a new named target if needed, D14). [owner] Name who receives it. Local check: tick --dry-run on a day with low stock builds and would send it, on a day without builds and sends nothing. Blocked by t17-cutover-b."
  llm: gpt-5.6-terra-medium
  status: pending
- id: t20-design-c
  content: "[agent] Design note for phase C: audiences in project.yml (owners, managers, admin, each a list of channel endpoints such as a Telegram chat secret or a Drive folder), a channel-neutral message (text, one image, documents, links), report YAML deliver by audience (deliver: [{ audience: owners, image: cover, documents: [pdf], text: weekly_caption, when: ... }]), publishing sinks (Drive) that run before messages and fill link placeholders, and ops alerts that go only to the admin audience through orchestration. Put the choices to the owner and record them. Local check: weekly.yml rewritten in the new form in the note, and the owner agreed. Blocked by t17-cutover-b."
  llm: claude-opus-5-5-high
  status: pending
- id: t21-channels
  content: "[agent] Implement phase C: distribution package with the message, a Channel protocol, the Telegram channel (from deliver/telegram.py) and the Drive sink; orchestration calls distribution only through it; the report schema takes audiences, refuses the old telegram and drive keys with a hint; weekly.yml and stock_alert.yml migrated; schemas and manifest regenerated. Local check: tick --dry-run builds both reports; the test suite has a fake channel receiving exactly the messages the old code sent; lint-imports shows distribution importing only core. Blocked by t20-design-c."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t22-ops-alerts
  content: "[agent] One alerts module for every pipeline step (a source failed, a data check error, a token close to expiry, a source not updated for N days), sent to the admin audience, de-duplicated per key and tick (D30: the 04:00 alert names the 06:00 retry, the 06:00 one says it was the last try). Local check: a tick with a failing fake source run twice at the same tick time sends one alert; tests for each alert kind. Blocked by t21-channels."
  llm: gpt-5.6-terra-medium
  status: pending
- id: t23-email
  content: "[agent] Optional, only when an audience asks for it: an email channel through the Gmail API with the existing Drive OAuth client (add the gmail.send scope, one re-consent by the owner). Local check: a dry run renders the email; one real send to the owner's address. Blocked by t21-channels."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t24-jibble-access
  content: "[owner] Jibble API access: confirm the Jibble plan includes API access; sign in as the organisation owner, Organization Settings, API Credentials, Create New Secret, name it hari-lari-analytics, and copy the Client ID and the Client Secret (shown once). Store them: printf %s '<id>' | gcloud secrets create jibble-client-id --data-file=- (same for jibble-client-secret), and in .env locally as JIBBLE_CLIENT_ID and JIBBLE_CLIENT_SECRET. Also answer: since when is attendance tracked in Jibble, who clocks in, are breaks clocked, and how is labour cost known (hourly rate or monthly salary per person, with dates). Report back: 'Jibble credentials stored' plus the answers. Can be done at any time."
  llm: "none (owner only)"
  status: pending
- id: t25-jibble-probe
  content: "[agent] Probe the Jibble API like extraction/moka/probe_schema.py did for Moka: a token from https://identity.prod.jibble.io/connect/token (client credentials), then People, TimeEntries (OData $filter on a time range, paging), and the timesheet and summary endpoints; time zones, edits and deletions, open entries, breaks, history depth and rate limits. Write docs/sources/jibble.md: the entities, the quirks, the tables to land, and whether to hand-write the extractor or use dlt's REST source. Raw responses stay under data/ (git-ignored). Local check: the doc names every table and column to land, with a real example per entity (names replaced). Blocked by t24-jibble-access."
  llm: claude-opus-5-5-high
  status: pending
- id: t26-dbt-spike
  content: "[agent] Spike on a branch: port stg_transactions, stg_transaction_lines, int_signed_lines and fct_sales_lines to a dbt-duckdb project, with sources, one seed, the checks of those models as dbt tests and models.yml as meta; compare the outputs with the current engine on the fixture and the run of 25 Nov; measure the build time, the image size and the CI time; show how the compiler and registry would read dbt's manifest instead of models.yml. Write the result and a recommendation into docs/decisions.md. [owner] Decide go or no-go (D24). Local check: the comparison is equal on all columns. Blocked by t17-cutover-b."
  llm: claude-opus-5-5-high
  status: pending
- id: t27-dbt-migrate
  content: "[agent] If t26 says go: move the warehouse to dbt-duckdb: all models, seeds, sources (the Moka tables of sources.yml), the data checks as generic and singular dbt tests, models.yml as meta read by the registry, and hari-lari-warehouse build running dbt in-process (dbtRunner). History models (the catalogue and stock over time) are built from the dated landing runs, not from dbt snapshot state, so the warehouse stays rebuildable. Local check: the weekly bundle.json on the warehouse is equal to the one before; dbt build passes; CI green. Blocked by t26-dbt-spike."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t28-jibble-extract
  content: "[agent] sources/jibble: the extractor chosen in t25 writes landing/jibble/<date>/ (Parquet and the manifest of t10), with retry, a timeout and a check that warns when no entries exist in the last 7 days; the tick runs it daily. Local check: hari-lari-extract-jibble --out data/landing writes a run the manifest schema validates; the tick dry run lists both sources. Blocked by t25-jibble-probe, t16-tick."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t29-jibble-models
  content: "[agent] Jibble models: staging per table, int_shifts (clock-in and clock-out pairs, breaks, shifts across midnight, the business day, open entries), dim_employee with a pseudonymous key, fct_shifts and fct_labour_hourly (labour hours per day and hour), the private seed staff_costs.csv (person key, hourly cost or salary rule, valid_from, as-of join). No employee name in a bundle, an Excel file or the marts unless the owner decides otherwise; staff names join the publish guard's denylist. Local check: data tests pass on the real landing run; the labour hours of one week match Jibble's own timesheet totals for that week. Blocked by t28-jibble-extract, t27-dbt-migrate (or t26 no-go)."
  llm: claude-opus-5-5-high
  status: pending
- id: t30-ops-mart
  content: "[agent] The first cross-source marts: fct_daily_ops (per day: net sales, orders, labour hours, labour cost) and fct_hourly_ops (per day and hour: orders, net sales, staff on shift); metrics labour_hours, labour_cost, labour_cost_pct, sales_per_labour_hour, orders_per_labour_hour; the KPI identity net sales = labour hours x sales per labour hour. The compiler stays one model per query. Local check: identity tests hold on the real run; hari-lari-component run with the new metrics prints plausible numbers checked by hand for one day. Blocked by t29-jibble-models."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t31-staffing-report
  content: "[agent] Components and reports/staffing.yml (absorbs old 10.10): labour cost % and sales per labour hour per week, staff on shift against orders per weekday and hour (heatmaps), calendar events marked; weekly to the owners audience. [owner] Read the first one and say what to keep. Local check: build with --strict, no overflow; the gallery renders the new components. Blocked by t30-ops-mart, t21-channels."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t32-margins
  content: "[agent] Old 10.4, steps pre to f (details in the archived plan, '10.4 Recipe margins'): [owner] share the Excel recipe workbook; read its structure, fix the columns of item_margins.csv and modifier_costs.csv (private config of t11), loaders with validation, int_item_costs (as-of join), cost columns on fct_sales_lines, the metrics cogs_idr, gross_margin_idr, gross_margin_pct and margin_coverage, the identity gross margin = net sales - COGS, reports/margins.yml, and scripts/export_item_margins.py. Always labelled recipe margin, always with coverage. Local check: margins of 5 to 10 items checked by hand against the workbook. Blocked by t11-private-config and the workbook."
  llm: claude-opus-5-5-high
  status: pending
- id: t33-margin-views
  content: "[agent] Old 10.5, 10.6 and 10.7: recipe margin by item, category and group (table and trend, with coverage), discount dependence with the margin given away per code, and the menu engineering matrix (popularity against margin, scatter_quadrant). Local check: build with --strict; numbers of one item checked by hand. Blocked by t32-margins."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t34-expenses-source
  content: "[agent] Old 10.9, first half: the expense tracker's production sheet as the source expenses: read-only extraction of the Expenses tab into landing/expenses/<date>/ (a service account with viewer access to the sheet), config/expense_group.csv mapping expense categories to P&L lines (private), stg_expenses and fct_expenses. [owner] Share the sheet with the service account as viewer. Local check: the monthly totals of fct_expenses equal the sheet's own totals for two months. Blocked by t16-tick and the expense tracker's go-live."
  llm: claude-opus-5-5-high
  status: pending
- id: t35-pnl
  content: "[agent] Old 10.9, second half: fct_daily_pnl (net sales, recipe COGS, labour cost, other expenses), the metrics prime_cost and prime_cost_pct (COGS plus labour over net sales) and profit_estimate, and reports/monthly_pnl.yml to the owners on the 2nd of each month. Labelled an estimate, with the margin coverage. Local check: one month reconciled by hand with the owner. Blocked by t33-margin-views, t34-expenses-source, t30-ops-mart."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t36-frontend-decision
  content: "[owner] Old 12.0: decide whether a frontend is wanted and what it must do, then hosting and sign-in: Telegram Mini App on a Cloud Run service that scales to zero (recommended: sign-in through Telegram, members checked against the group), Cloud Run with Google sign-in, or Cloudflare Workers static assets behind Cloudflare Access (free up to 50 users, data leaves Google Cloud). Report back: the choice. Best after t44-owner-feedback."
  llm: "none (owner only)"
  status: pending
- id: t37-frontend-mvp
  content: "[agent] Old 12.3: a static, mobile-first app in frontend/ (Vite, one chart library chosen in t36) that renders bundle.json: insights first, the pages as stacked cards, web versions of the visual types; a week picker over reports/index.json and a period switch over the periods in the bundle. No metric logic in JavaScript: display strings only, raw values only to draw. Local check: every gallery bundle renders without console errors (headless Playwright, old 12.6). Blocked by t36-frontend-decision, t15-ledger."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t38-frontend-hosting
  content: "[agent] Old 12.4 and 12.6: hosting and sign-in as decided in t36; for the Mini App one Cloud Run service (max one instance) serves the app, checks Telegram's initData signature, checks group membership (cached) and returns bundles from the bucket; the caption gets the link. Tests: unsigned requests and non-members are refused; no bundle or mart enters git. [owner] Open it from the group on the phone. Local check: the tests, and the bill after the first month. Blocked by t37-frontend-mvp."
  llm: claude-opus-5-5-high
  status: pending
- id: t39-publish-decision
  content: "[owner] Old 11.0: decide (a) whether the name Hari Lari stays in code and README (recommended: yes, publish no data), (b) which docs stay public (recommended: README, METRICS.md, docs/components.md, docs/architecture.md, docs/decisions.md and a generic runbook; the plans stay private), (c) rewrite this repository's history once or publish a fresh one (recommended: rewrite once), (d) the owners agree. Report back: the four answers."
  llm: "none (owner only)"
  status: pending
- id: t40-demo-cafe
  content: "[agent] Old 11.3 and 11.4: scripts/make_demo_data.py writes a reproducible fictional café in the landing shape of every source (Moka, and Jibble once it exists) plus config/examples/; tests, gallery, snapshots and README screenshots use it; real names in tests and fixtures (partners, discount codes, a first name) become fictional names of the same shape. Local check: the suite passes on the demo data and fails for the same reasons as before when a rule is broken. Blocked by t39-publish-decision, t11-private-config."
  llm: claude-opus-5-5-high
  status: pending
- id: t41-publish-guard
  content: "[agent] Old 11.5 to 11.7: clean README, METRICS.md and docs of real figures (the worked example recomputed on the demo café); identifiers become settings (.env, neutral .env.example, no default email in deploy/setup.sh); scripts/publish_check.py scans the working tree and all refs for a denylist kept outside git and for patterns (emails, IDR amounts, token shapes, gs:// paths, Moka ids), as a pre-push hook and a CI step, and also covers frontend/. Local check: publish_check.py on the current history lists the known hits; on a clean branch it passes. Blocked by t40-demo-cafe."
  llm: claude-sonnet-5-5-high
  status: pending
- id: t42-history-rewrite
  content: "[agent] Old 11.8 and 11.9: git filter-repo with --invert-paths for files that left git (config CSVs, plan files) and --replace-text with the denylist, across every branch; force-push. [owner] Re-clone everywhere, switch on secret scanning and push protection, read the README as a stranger, get the owners' final OK, make the repository public. Local check: publish_check.py reports zero hits on all refs; the suite passes. Blocked by t41-publish-guard."
  llm: claude-opus-5-5-high
  status: pending
- id: t43-daily-flash
  content: "[agent] Old 10.17: reports/daily.yml, yesterday against the same weekday a week earlier plus the insights, as Telegram text only, daily to the managers audience. With the tick it is one YAML file and no deploy change. [owner] Name the managers' chat. Local check: tick --dry-run on two consecutive dates builds it each day. Blocked by t21-channels."
  llm: gpt-5.6-terra-medium
  status: pending
- id: t44-owner-feedback
  content: "[owner] Old 10.0, around 21 Dec after four live Mondays: ask the owners which pages they read, which numbers they acted on, what they missed, their targets, and which insight sentences they trusted or found noisy. Record the answers in this plan and reorder the backlog. Report back: the answers."
  llm: "none (owner only)"
  status: pending
- id: t45-weekly-v2
  content: "[agent] Old 10.1 and 10.2: a first page 'This week' (the top insights across pages, the net sales drivers along the KPI tree, KPI cards against target, later margin and labour cost), the period pages trimmed to what t44 shows is read, the top three insights in the caption; config/targets.csv (private) with target lines on kpi_card and bar_trend and a share-of-target insight rule. Local check: build with --strict; the owners read one in the admin chat first. Blocked by t44-owner-feedback, t11-private-config."
  llm: claude-opus-5-5-high
  status: pending
isProject: false
---

# Hari Lari analytics platform

The go-live plan ([archived](archive/hari_lari_analytics_go-live_47a99005.plan.md)) built the extraction, a reporting framework and the live weekly report, and closed after phase 9.8 on 26 Nov 2026. This plan turns that repository into a small modular BI platform: clear module boundaries, a stored warehouse between the data and the reports, delivery through audiences instead of hard-wired channels, and new sources: Jibble attendance first, then margins and expenses. The hard constraint is cost: under 10 EUR a month, and the expected cost is under 1 EUR.

Phases A to C restructure; each ends with outputs identical to before (A) or with one visible payoff (B: daily data and a daily stock alert; C: a new audience or channel is config). Phase D is the test of the structure: a second source has to fit without touching the report engine. E adds the money, F the frontend, G the publishing.

Critical path: t1 Monday → t5 → t6 → t7 → t8 → t9 → t10 → t12 → t13 → t14 → t15 → t16 → t17 → t26 → t27 → t28 → t29 → t30 → t31. The owner tasks marked "can be done at any time" (t24) run in parallel.

```mermaid
graph TD
  P0["Phase 0: close out, safety, architecture doc"] --> PA["Phase A: module boundaries, same outputs"]
  PA --> PB["Phase B: daily tick, warehouse, run ledger"]
  PB --> PC["Phase C: audiences and channels"]
  PB --> PD["Phase D: Jibble and dbt-duckdb"]
  PC --> PD2["staffing report"]
  PD --> PD2
  PB --> PE["Phase E: margins, expenses, prime cost"]
  PD --> PE
  PB --> PF["Phase F: frontend over bundles"]
  PA --> PG["Phase G: publishing"]
  PB --> BL["Backlog: daily flash, weekly v2, targets"]
```

## Rules for this plan

- **No release between Saturday 18:00 and Monday 05:00 WITA**, and every release goes through the cloud dry run (7A.8). After phase B every day is a run day, so the rule becomes: release Tuesday to Thursday, and watch the next tick (phase B itself is released on Friday 27 Nov, see the exception below).
- **Exception for phase B (decided 27 Nov 2026, a Friday): one release of everything up to t18, and Monday 30 Nov is the live test of it all.** The owner works through t11 to t18 with the agents today, 27 Nov, and releases the result once, before the freeze starts on Saturday 18:00 WITA. The Docker image promoted for it (Artifact Registry, the Cloud Run job on Google Cloud) contains phase 0 (t1 to t4, done), phase A (t5 to t9, already live as `bd35ece9d9f7`) and phase B (t10 to t18): config from the bucket, landing, warehouse, reports reading the warehouse, ledger, `hari-lari-tick`, runbook and cost hygiene. Cloud Scheduler then runs `hari-lari-tick` on that image instead of `hari-lari-report-weekly`; the daily ticks of Saturday 28 and Sunday 29 Nov (no report due) are the first signals, and **Monday 30 Nov, 04:00 WITA, is the first live run of phases 0, A and B together**, with the owner reading the report at 04:10 (t1's routine, t9's bundle compare, t17's confirmation). This departs deliberately from "release Tuesday to Thursday". Only one part of t18 cannot be done today: reading the bill after the first month on the tick (about 1 Jan 2027); it is a dated follow-up in 'Cost budget', not a reason to hold the release. The fallback if t17 is not verified, with the promoted image tag recorded in it, by Saturday 18:00: keep `hari-lari-report-weekly` as the Monday job on the last image, leave the tick unscheduled (or `--dry-run` only), and release phase B Tuesday to Thursday as the rule says.
- **Statuses stay one line.** What was done, measured and found goes into the pull request description and the commit messages, not into this file.
- **Each phase ends with a check on real data**, named in its last todo.

## What was found before planning (26 Nov 2026)

- **The report run owns the pipeline.** `run_report_job` in `reporting/delivery.py` extracts, builds the models, checks, renders, stores, uploads to Drive, sends to Telegram and writes the marker. Each scheduler job runs `run --report <id>` with its own full Moka pull; only `run --scheduled` (the release dry run) shares one extraction. Data freshness is tied to the report schedule, and a second source has nowhere to plug in.
- **No warehouse, only "the latest run".** `open_run()` loads the newest extraction folder and rebuilds all 16 models in memory. The single-source assumption sits in the names (`raw_<table>`, `sources.yml` is the Moka table list, `load_run` lives in the legacy `load_api.py`). Catalogue and stock snapshots exist in the dated `raw/` folders but are not modelled as history.
- **`reporting/` holds 14,300 of 17,200 Python lines** and contains transformation, the semantic layer, the query engine, presentation, report definitions, outputs and orchestration.
- **The pandas pipeline is still load-bearing** (about 1,200 lines in `load.py`, `load_api.py`, `clean.py`, `dimensions.py`, `periods.py`, `metrics.py`, `report_excel.py`): the cleaning rules exist twice, and the new core imports the old modules.
- **Channels are written into the report schema** (`deliver: { telegram, drive }`), ops alerts share the delivery path, and the expense tracker has its own Telegram client.
- **Drive is a publishing sink, not the archive.** Cloud Storage is the archive. Drive depends on a personal Gmail OAuth refresh token, the most fragile credential.
- **What happened in a run is scattered** over sent-markers, output folders, JSON logs and Cloud Run executions.
- **Proportion:** the framework is complete for sales views (new components took 2 to 4 minutes in YAML in 9.1). The next value is new data, so the restructure is sized to make new sources cheap.
- **Cloud Scheduler:** 3 free jobs per billing account. The expense tracker already uses one (`expense-bot-tick`), the weekly report one more. One job per report would exceed the free jobs with the stock alert and a daily flash.
- **Jibble:** an organisation owner creates API credentials (client id and secret, OAuth client credentials at `identity.prod.jibble.io`); the REST API at `workspace.prod.jibble.io/v1/` serves `People` and `TimeEntries` with OData filters. Third-party docs say the free plan includes API access; t24 confirms it for the account.

## Decisions (26 Nov 2026)

- **D20. Break off.** The go-live plan closes after 9.8 and is archived; its open items are carried over (table below).
- **D21. dbt-core with dbt-duckdb for the warehouse, adopted when the second source lands** (spike t26, go or no-go D24). The semantic layer, the KPI tree, the compiler, transforms, insights and the presentation stay our own: that is the part no tool does better for this café. D8 rejected dbt for cost; dbt-core is free and runs in-process, so the trade-off is one dependency against snapshots of history, sources and freshness, multi-source staging, lineage, and a manifest that Lightdash, Evidence and MetricFlow read.
- **D22. One daily tick.** One Cloud Scheduler job starts one Cloud Run job a day: extract the sources that are due, build the warehouse, then run the reports that are due. A report's `schedule` says on which days it is due, the tick says when.
- **D23. Jibble is the first new source**, because it unlocks labour cost % and sales per labour hour, which no Moka data can.
- **D25 (27 Nov 2026, t3). Insight rules, facets and the semantic manifest belong to `presentation`**, not `analysis`, where each would import upwards. All decisions are now in [docs/decisions.md](decisions.md).
- **Principles:** the warehouse holds nothing that cannot be rebuilt from landing and config; `distribution` and `sources` import only `core`; reports name audiences, never channels; a frontend renders bundles and never computes a metric.

## Target architecture

### Modules

Each module may import only the modules above it in this list; `sources` and `distribution` import only `core`; `orchestration` is the one module that wires everything. Every module is either an **engine** (Python, changes rarely) or holds **content** (SQL, YAML, CSV, changes weekly).

| Module | Responsibility | Content it reads |
| --- | --- | --- |
| `core` | settings (`project.yml`), secrets, storage (local or `gs://`), logging, clock | `project.yml` |
| `sources/<name>` | one extractor per source system, writes `landing/<source>/<date>/` and nothing else | |
| `warehouse` | staging per source, history models, marts, seeds, data tests; dbt-duckdb from t27 | SQL models, `sources.yml`, `models.yml`, config CSVs |
| `semantic` | metrics, dimensions, KPI tree, model metadata, semantic manifest | `metrics.yml`, `dimensions.yml`, `kpi_tree.yml` |
| `analysis` | query compiler, windows, transforms, decompose, `@query` sources | |
| `presentation` | components, facets, insight rules (D25), visuals, layouts, formats, report engine and renderers (PDF, PNG, XLSX, text, bundle), semantic manifest | `components/*.yml`, `reports/*.yml`, `insights.yml` |
| `orchestration` | the daily tick, due rules, run ledger, idempotency, ops alerts, CLIs that run things | |
| `distribution` | channels (Telegram, later email) and publishing sinks (Drive) behind one message type | audiences in `project.yml` |
| `frontend/` | static app over `bundle.json` and `reports/index.json` | |

### Storage layout (one bucket, `gs://hari-lari-data`)

```text
landing/<source>/<date>/      Parquet tables, manifest.json, raw JSON (deleted after 90 days)
warehouse/<date>/             warehouse.duckdb, marts/<model>.parquet
warehouse/latest.json         the newest good build
reports/<report>/<anchor>/    PDF, PNG, XLSX, text, bundle.json
reports/index.json            every report and anchor, for the frontend
runs/<date>/<run>-<step>.json the run ledger
config/                       private business config (aliases, groups, margins, targets, staff costs)
```

### A day

At 04:00 WITA, with a retry tick at 06:00 (D27), Cloud Scheduler starts the job. It extracts each source that is due into `landing/`, builds the warehouse and runs the data tests, writes the marts, runs every report whose schedule is due today and whose sources succeeded, publishes through the audiences, and writes a ledger record per step. A failed source alerts the admin; reports that do not need it still run; a report still not sent after the last tick is announced to its chats and sent by hand (D30). The details are in [architecture.md](architecture.md).

### End-state warehouse

- **Dimensions:** `dim_date` (trading days, holidays, closures), `dim_item` with its history from the daily catalogue snapshots (renames and Moka's reuse of category ids handled properly), `dim_category` and groups, `dim_employee` (pseudonymous), `dim_supplier`, `dim_expense_category`.
- **Facts:** `fct_sales_lines`, `fct_orders`, `fct_stock_daily`, `fct_shifts`, `fct_labour_hourly`, `fct_expenses`, item costs as of each sale.
- **Cross-source marts:** `fct_daily_ops`, `fct_hourly_ops`, `fct_daily_pnl`. Here the café's key numbers appear: labour cost % of sales, sales per labour hour, staffing against demand by hour, prime cost % of sales, and a monthly profit estimate.

## Cost budget

Approximate list prices; t18 replaces the estimate with the real bill, read after the first month on the daily tick (about 1 Jan 2027).

| Item | Per month | Note |
| --- | --- | --- |
| Cloud Run job, one tick a day, about 3 to 5 min | 0 | inside the free tier |
| Cloud Run service for the frontend (phase F) | 0 | scales to zero |
| Cloud Storage, `asia-southeast2` | under 0.20 EUR | the free 5 GB is only in US regions; a few GB a year |
| Secret Manager | about 0.4 EUR | 13 secrets; 6 active versions free per billing account, then 0.06 USD each; enabled and disabled versions both count, only destroyed ones are free (t18, 27 Nov) |
| Artifact Registry | about 0.45 EUR | 5.5 GB on 27 Nov, 0.5 GB free, 0.10 USD per GB; the policy keeps 20 versions (t18, 27 Nov) |
| Cloud Scheduler | 0 | 2 of the 3 free jobs (the tick and the expense tracker's) |
| GitHub Actions, Cloudflare | 0 | inside the free tiers |
| **Total** | **about 1 EUR** | the 5 USD budget alert stays |

## Carried over from the go-live plan

| Old | New | Note |
| --- | --- | --- |
| 6.5 first live Monday | t1-monday | |
| 6.6 runbook | t17-cutover-b | rewritten for the tick |
| 6.7 remove the CSV source | t6-retire-pandas | together with the whole pandas pipeline |
| 10.0 owner feedback | t44-owner-feedback | |
| 10.1, 10.2 weekly v2, targets | t45-weekly-v2 | |
| 10.4 to 10.7 margins | t32-margins, t33-margin-views | |
| 10.9 expenses and profit | t34-expenses-source, t35-pnl | |
| 10.10 staffing | t31-staffing-report | now with Jibble labour hours |
| 10.17 daily flash | t43-daily-flash | |
| 11.0 to 11.9 publishing | t2, t11, t39 to t42 | 11.1 is urgent (t2), 11.2 moves into phase B (t11) |
| 12.0 to 12.6 frontend | t13 (marts), t15 (index), t36 to t38 | 12.5 interactive queries stays optional, below |

## Backlog (unscheduled; t44 reorders it)

- **10.3** Unpriced add-ons from line notes (`config/note_addons.csv`), to see whether the new modifiers stopped it.
- **10.8** Payment mix with estimated card fees (`config/payment_fees.csv`).
- **10.11** Refund and void log.
- **10.12** Penetration and category AOV trend per category.
- **10.13** Basket size distribution and its trend.
- **10.14** Item Pareto (ABC).
- **10.15** New, rising and fading items.
- **10.16** Category hygiene as a data note.
- **10.18** BigQuery and Looker Studio over the marts: only if the owners want self-service, compared with the frontend first.
- **10.19** Stock alert as an order list per supplier (after t19).
- **12.5** Interactive queries on the frontend through the same compiler over the marts, only if the bundles are not enough.
- **Shared Telegram client** with the expense tracker, if both keep growing.

## Risks

- **The restructure breaks the Monday report:** phase A changes no output and is checked by identical bundles and pixel-identical pages (t6, t7); B and C release midweek after a dry run and a run to the admin chat (t17). Phase B is the exception (27 Nov 2026): t11 to t18 are released together on a Friday, in one image with phases 0, A and B, so that Monday 30 Nov is a live test of all of it; the cloud dry run, the admin-chat tick and the rollback command are the safety net, and if they are not clean by Saturday 18:00 the old weekly job stays (see 'Rules for this plan').
- **The refactor grows without paying off:** every phase after A ends with one visible payoff; phase D measures whether a new source fits without touching the report engine.
- **dbt adds more weight than it saves:** the spike (t26) is a go or no-go with measured build time, image size and CI time.
- **Labour data is personal:** pseudonymous keys, no names in bundles, Excel or marts unless the owner decides, staff costs only in private config (t29).
- **A source fails silently:** the ledger records every step, freshness alerts fire after N days without data (t22), and reports that need a failed source are not sent.
- **Margins or profit read as exact:** labelled recipe margin and estimate, always with coverage (t32, t35).
- **Business data leaks once public:** private config (t11), publish guard on every push (t41), history rewritten once and checked on all refs (t42).

## Models per task

- **claude-opus-5-5-high:** design notes, data logic with traps (shifts, as-of joins), parity and golden files, security-relevant work.
- **claude-sonnet-5-5-high:** standard implementation with tests.
- **gpt-5.6-terra-medium:** small, well-specified changes.
- **gpt-5.4-nano-medium:** mechanical edits, docs and housekeeping.
- **Owner:** anything that needs a login, a decision or a phone.
