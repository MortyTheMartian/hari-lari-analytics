# Runbook: Hari Lari analytics pipeline

What runs every day, how to run it by hand, how to re-run a day, what each admin alert means,
how a release reaches Google Cloud and how to roll it back. The daily flow is described under
"The daily tick"; the cut-over to it (plan t17, 27 Nov 2026) under "The cut-over to the daily
tick".

## Telegram

### Where things live

| What | Where |
| --- | --- |
| Bot token | Secret Manager `telegram-bot-token` (project `hari-lari`) |
| Group chat id | Secret Manager `telegram-chat-id` |
| Admin chat id (alerts, private run) | Secret Manager `telegram-admin-chat-id` |

The bot is a separate bot from the expense-tracker bot (which has a webhook set; a bot with a
webhook cannot use `getUpdates`). Do not reuse the expense-tracker secrets `TELEGRAM_BOT_TOKEN`
(upper case) for this job.

### The chat id changes when the group becomes a supergroup

A new Telegram group starts as a basic group with a negative id such as `-4012345678`. Telegram
converts it to a **supergroup** automatically when, for example, you make it public, change its
history visibility, add many members or promote an admin. The id then becomes `-100...`
(for example `-1004012345678`), and the old id stops working: sends fail with
`Bad Request: group chat was upgraded to a supergroup chat` (the error response carries
`parameters.migrate_to_chat_id` with the new id).

If the weekly report stops arriving and the admin chat shows that error:

1. Get the new id: write a message in the group, then run `uv run hari-lari-telegram chat-ids`
   (the group shows up with type `supergroup`).
2. Store it: `deploy/setup.sh telegram-chat group <new id>`.
3. Re-send the missed report: `hari-lari-pipeline tick --date <the Monday it was due> --only weekly
   --force` (see "Re-running a day").

Tip: make the group a supergroup right away (for example by turning "Chat history for new
members" to visible), then read the id, so the stored id is the final one.

### Useful commands

```bash
uv run hari-lari-telegram whoami      # does the stored token work?
uv run hari-lari-telegram chat-ids    # which chats can the bot see (id, type, name)
uv run hari-lari-telegram ping --to admin   # test message to the admin chat
uv run hari-lari-telegram ping --to group   # test message to the group
```

The bot only sees messages it is allowed to see. In a group, turn privacy mode on (the default)
and write `/start@<botname>` or reply to the bot, or make the bot an admin, so `chat-ids` can
find the group.

## The pipeline (`hari-lari-pipeline tick`, `hari-lari-report run`)

Since the cut-over (27 Nov 2026) one Cloud Scheduler job, `hari-lari-tick`, starts the Cloud Run
job `hari-lari-weekly` every day at 04:00 and 06:00 WITA with the arguments `tick --scheduler`
(the job keeps its old name; it runs every report). The tick extracts, builds the warehouse and
runs the reports that are due; the reports of `src/hari_lari_analytics/presentation/reports/` are
files, not jobs. `hari-lari-report run --report <id>` still runs one report on its own, for a
run by hand; the image also answers the old arguments (`run --report weekly ...`).

### The daily tick

`hari-lari-pipeline tick` is the job Cloud Scheduler starts as `hari-lari-tick`, at 04:00 and
06:00 WITA (`tick.times` in `project.yml`). It extracts Moka unless it already landed today,
builds the warehouse unless today's is up to date (built by this image, from the newest landing
and the current config files, so an upload to `config/` or a new release means a rebuild), and
runs every report whose cron day fields match today (weekly: Mondays). On a day with nothing
due, such as a Saturday, it only lands the data and builds the warehouse. The 06:00 tick does
only what the 04:00 one left undone. A failing source or warehouse skips the reports that need
it (alert: "the 06:00 tick tries again" or "this was the last tick today"); one failing report
does not stop the others. After the 06:00 tick, a report still not sent is announced to its own
chats as late, and the admin gets the command that sends it:

```bash
uv run hari-lari-pipeline tick --date 2026-11-30 --only weekly   # a late report, by hand
uv run hari-lari-pipeline tick --dry-run --date 2026-11-30      # what Monday would do
uv run hari-lari-pipeline tick --to admin --all-scheduled       # every scheduled report, privately
uv run hari-lari-runs list --since 2026-11-30                   # what each tick did
```

`--to admin` and `--dry-run` never count as sent and never announce a late report.

The tick's own options are `--date` (the due date: which reports are due and their anchor;
default today in Bali), `--only <report>`, `--all-scheduled` (every report with a schedule counts
as due, as in the release's dry run) and `--scheduler` (set by Cloud Scheduler: the run is recorded
as scheduled and the last tick of the day announces a late report). It shares `--dry-run`, `--to`,
`--force`, `--data-root`, `--config` and `--drive-folder-id` with the report command, whose
options are:

| Option | Meaning |
| --- | --- |
| `--report <id>` | The report to run (`weekly`, `stock_alert`, ...). |
| `--scheduled` | Every report with a schedule, one after the other on one extraction (the release's dry run). |
| `--dry-run` | Everything except sending: no Telegram message (also no alert), no Drive upload, no marker. Files go under `dry-run/` in the data root; the log shows what would be sent to whom. |
| `--to admin` | A private run: every Telegram delivery goes to the admin chat. Not marked as sent, so it never blocks the real send. Still uploads to Drive. |
| `--anchor YYYY-MM-DD` | Last day of the report (default: yesterday in Bali). |
| `--force` | Send even if this report was already sent for this anchor. |
| `--warehouse <date\|path>` | Use an existing warehouse instead of extracting and building again: a run date (`warehouse/<date>/` in the data root), or the path or `gs://` URL of a `warehouse.duckdb`. |
| `--data-root` | Local folder or `gs://bucket`; env `DATA_ROOT`, else `data`. |

Files in the data root:

| Path | What |
| --- | --- |
| `landing/moka/<run date>/` | The extraction: Parquet tables, `manifest.json`, gzipped raw JSON |
| `warehouse/<run date>/`, `warehouse/latest.json` | The models built from it (`warehouse.duckdb`), the marts, `build.json` |
| `reports/<report>/<anchor>/` | The PDF, the PNG pages, the Excel workbook, the Telegram texts, `bundle.json` |
| `runs/<run date>/<run id>-<step>.json` | The run ledger: one record per extraction, warehouse build and report, with its deliveries. A `sent` record (not a dry run, not `--to`) means "this report went out for this anchor" |
| `reports/index.json` | Every report and anchor that has outputs, regenerated after each report run |
| `sent/<report>/<anchor>.json` | The sent-marker, still written next to the ledger until phase B is confirmed; also read as "already sent" |
| `reports/<anchor>/`, `sent/<anchor>.json` | The same, written by the old weekly command before the cut-over (9.7); the weekly report also respects that old marker |
| `dry-run/...` | The same layout, written by dry runs |

### Running it by hand

```bash
# locally (secrets through gcloud; the Moka token from Secret Manager needs
# `gcloud auth application-default login` once)
uv run --extra gcp hari-lari-report run --report weekly --dry-run \
  --token-store secret --gcp-project hari-lari
# on a warehouse you already have, without calling Moka
uv run hari-lari-warehouse build            # data/landing -> data/warehouse/<today>/
uv run hari-lari-report run --report weekly --dry-run --warehouse "$(date +%F)"
# in the cloud: every scheduled report to the admin chat only (what t17 did on 27 Nov)
gcloud run jobs execute hari-lari-weekly --region asia-southeast2 --project hari-lari \
  --args=tick,--to,admin,--all-scheduled --wait
# other cloud runs: --args=tick,--dry-run,--all-scheduled   (sends nothing)
#                   --args=tick,--date,2026-11-30,--only,weekly   (a late report, sent for real)
#                   --args=run,--report,weekly,--to,admin         (one report, the old form)
```

`--args` replaces the job's arguments for that one execution only. The job has no arguments of
its own: started without `--args` it only prints the usage and fails. A `--to admin` run still
uploads to Drive, so a folder for that anchor appears next to the real ones (it is shared like
them); the sent record is not written, so the real run is not blocked.

Every run writes the run ledger, one JSON record per step under `runs/<run date>/` (dry runs
under `dry-run/runs/`). To see what ran, what was sent to whom and what failed:

```bash
uv run --extra gcp hari-lari-runs list --data-root gs://hari-lari-data --since 2026-11-30
uv run hari-lari-runs list --report weekly          # local: data/runs/
```

Logs are JSON lines. In Cloud Logging, filter by step:
`resource.type="cloud_run_job" jsonPayload.step="deliver"`.

### Re-running a day

A day is one tick date. The tick is idempotent: it reuses what the earlier tick of the day left
(the landing, an up-to-date warehouse) and skips a report that was already sent, so running it
again is safe. The by-hand form names the day and, if needed, the report:

```bash
gcloud run jobs execute hari-lari-weekly --region asia-southeast2 --project hari-lari \
  --args=tick,--date,2026-11-30,--only,weekly --wait
```

* **A tick failed and the next one is coming:** do nothing. The 06:00 tick retries what the
  04:00 one left undone; the alert says so ("The 06:00 tick tries again").
* **It says "already sent":** the ledger has a `sent` record for that report and anchor (a
  record that is not a dry run and not `--to`), or the old sent-marker exists. Use `--force`
  only if you want the group to get the report a second time.
* **Cloud Run retried after a failure:** nothing was sent twice. A report counts as sent from
  its ledger record, written after the send; a failed run sends the admin alert and leaves no
  `sent` record.
* **The send failed but the files are fine:** they are in `reports/<report>/<anchor>/`. Run the
  command above. The landing of the day is reused, so it takes the report and the sending only:
  the admin-chat tick of 27 Nov took 154 s, 124 s of it sending.
* **A source or the warehouse failed and the reports were skipped:** fix the cause (a token, a
  config file), then run the tick for that date as above; the ledger shows the skipped reports
  (`hari-lari-runs list --since <date>`).
* **The day's data itself must be fetched again** (a late correction in Moka): the tick does
  not extract a source that already landed on the run date. Run the extraction by hand into the
  bucket, which overwrites that day's landing (the old manifest is deleted before the tables are
  written), then the tick; the new landing run makes it rebuild the warehouse. Derived from the
  code, not rehearsed:

  ```bash
  uv run --extra gcp hari-lari-extract --token-store secret --gcp-project hari-lari \
    run --out gs://hari-lari-data/landing
  ```
* **The business config changed:** upload it (see "The business config"); the next tick sees the
  changed files and rebuilds the warehouse before it runs the reports.

### What the admin chat tells you

| Message | Meaning and action |
| --- | --- |
| `Hari Lari tick, source moka failed at step: extract` | The Moka extraction failed (API down, a 401 from the token, a failing sanity check). Every report that needs it is skipped and recorded as `skipped`; nothing reached any group. The message ends with "The 06:00 tick tries again." (sent by the 04:00 tick) or "This was the last tick today." (the 06:00 one, or a run by hand: "run the tick again to retry"). For a 401, see "Re-authorising the Moka token". |
| `Hari Lari tick, warehouse failed at step: warehouse` | The warehouse build failed: a required config file is missing from `gs://hari-lari-data/config/` (the message names it), or an error-level data check (`checks.yml`) failed and names the rows. The reports are skipped as above, with the same retry line. Fix the file or the data, then re-run the day. |
| `Hari Lari report <id> failed at step: X` | Step X (`check`, `model`, `report`, `upload`, `deliver`) failed and that report ended with an error; the other due reports still ran. `model` opens the warehouse and runs the report's queries. Open the logs link, fix, re-run the day with `--only <id>`. Nothing reached the group unless X is after `deliver`. (A by-hand `hari-lari-report run` that extracts or builds the warehouse itself reports those steps under this name too.) |
| `Hari Lari report <id> for <anchor> was not sent by the last tick (<reason>). Its chats were told it is late. Send it with: hari-lari-pipeline tick --date <day> --only <id>` | The 06:00 tick ended and the report is still not sent (D30). The report's own chats got "<title> up to <date> could not be sent this morning. It follows later today." Fix the cause in the reason, then run the command; there is no automatic catch-up on the next day. Only a scheduled tick sends this: not `--dry-run`, not `--to`, not a run by hand. |
| `step drive failed ... without the Drive link` | The Drive upload failed. The group still got the cover and the PDF, just no link. Run `hari-lari-drive whoami`; if it says `invalid_grant`, redo the consent (see Drive below). |
| `step marker failed ... could not be written` | The report was sent and the ledger says so, so a re-run does not send it again; only an image from before the ledger (after a rollback) would. Create `sent/<report>/<anchor>.json` by hand before such a rollback. |
| `the ledger record ... could not be written` | The step ran, but its record is missing. If it was a report that went out, a re-run would send it again unless its sent-marker exists (`sent/<report>/<anchor>.json`). |
| `The Moka API token expires on ...` | See below. |

### The cut-over to the daily tick (plan t17, 27 Nov 2026)

One release did it: the merge of pull request 12 (phases B, t11 to t16) built the image, ran
the dry run `tick --dry-run --all-scheduled`, promoted it and synced the scheduler. Nothing was
switched by hand, and the order is the release script's: promote, then schedules, so the tick
exists as soon as the workflow is green.

| What | After the release |
| --- | --- |
| Live job `hari-lari-weekly` and `hari-lari-weekly-dryrun` | image `df650ed5e4d3` (phases 0, A and B); label `previous-release` is `bd35ece9d9f7` |
| Scheduler | `hari-lari-tick`, `0 4,6 * * *` Asia/Makassar, starts the live job with `tick --scheduler`; the expense tracker's `expense-bot-tick` is untouched; `hari-lari-report-weekly` was deleted by the release |
| Private run | `tick --to admin --all-scheduled` on the live job: report, cover and PDF in the admin chat, Drive upload done, 154 s; ledger run `203436-7dab` (`extract` was skipped, the landing of 27 Nov was already there, so the first extraction by the live job is the 04:00 tick of the next day) |

To check where things stand:

```bash
deploy/release.sh status                       # the image of both jobs and the previous release
gcloud scheduler jobs list --location asia-southeast2 --project hari-lari   # tick + expense tracker's job only
uv run --extra gcp hari-lari-runs list --data-root gs://hari-lari-data --since 2026-11-28
```

A healthy day with nothing due leaves two ledger records from the 04:00 tick (`extract-moka`,
`warehouse`) and none from the 06:00 one, which finds the day's landing and an up-to-date
warehouse and does nothing. On Monday the weekly report adds `report-weekly`. (Read from
`tick.py`; the ledger of Saturday 28 Nov is the first live check of it.)

**Rolling back across it.** `deploy/release.sh rollback` promotes `bd35ece9d9f7`, the last image
before the tick, recreates `hari-lari-report-weekly` (Monday 04:00 WITA, `run --report weekly`)
and deletes `hari-lari-tick`; it needs `docker` and a `gcloud` login. That image reads the
config baked into it, not `gs://hari-lari-data/config/`, so config edited since the release is
not in effect there, and it does not read the ledger. It still reads the sent-markers, which the
tick keeps writing next to the ledger, so a report the tick sent is not sent again. Write
`sent/<report>/<anchor>.json` by hand if a marker is missing before rolling back.

### The cut-over to the report command (plan 9.7, done 25 Nov 2026)

Until 9.7 the image ran the old command `hari-lari-weekly` with the old report, started by the
scheduler job of the same name. Since then the image's entry point is `hari-lari-report` and the
weekly report is a component report. The cut-over: `deploy/setup.sh report-scheduler`, the
release that created `hari-lari-report-weekly`, the old scheduler job paused, a private run to
the admin chat, and then the old scheduler job and the old report's code deleted (9.7b). There
is no fallback to the old report: a bad release is rolled back to an image from after the
cut-over (see "Release path").

### Re-authorising the Moka token

The token is refreshed automatically when fewer than 30 days remain, and the refresh is saved to
Secret Manager. The reminder (fewer than 45 days left) only shows up when that did not extend it.
To start from scratch:

```bash
uv run --extra gcp hari-lari-extract auth url          # open the printed link, allow, copy the redirect URL
uv run --extra gcp hari-lari-extract --token-store secret --gcp-project hari-lari auth exchange "<redirect URL>"
```

The second command adds a new version of the secret `moka-oauth-token`; the next run uses it.

### The business config (`CONFIG_DIR`)

The CSV files the models read (`item_aliases.csv`, `category_group.csv`,
`calendar_events.csv`) are private: they are neither in git nor in the image. The jobs read them
from `gs://hari-lari-data/config/` (their variable `CONFIG_DIR`, set by `deploy/setup.sh` and on
every release); locally the default is `config/`, which git ignores. Each file is required: a
missing one fails the run at step `warehouse` with the admin alert. `config/examples/` in git has the
same columns with demo values.

To change one, edit it in `config/`, build the report locally on it, then upload it; the next
run reads it, no release needed:

```bash
uv run hari-lari-warehouse build && uv run hari-lari-report build --report weekly
gcloud storage cp config/*.csv gs://hari-lari-data/config/
```

A fresh checkout gets the real files back with `gcloud storage cp gs://hari-lari-data/config/*.csv config/`.

### Drive

A service account cannot create files in a normal Google Drive ("Service Accounts do not have
storage quota"). So the job uploads **as harilari.bali@gmail.com**, using a one-time consent:

| What | Where |
| --- | --- |
| OAuth client id and secret | Secret Manager `drive-oauth-client-id`, `drive-oauth-client-secret` (OAuth client "Hari Lari job" in the Cloud console, project `hari-lari`) |
| Refresh token of the Google user | Secret Manager `drive-oauth-refresh-token` |
| Target folder of the weekly job | env `DRIVE_FOLDER_ID` of the Cloud Run job ("Weekly Sales Reports") |

Each week gets a sub-folder `<anchor> (week N)` with the PDF and the Excel lines; its link goes
into the caption. The sub-folders inherit the sharing of the target folder, so add and remove
members there. Another report job gets its own `DRIVE_FOLDER_ID` and uses the same OAuth secrets.

Commands: `hari-lari-drive whoami` (does the token work?), `hari-lari-drive folder <id>`,
`hari-lari-drive auth-url` and `hari-lari-drive exchange "<URL>"` (redo the consent).

If the Drive upload fails with `invalid_grant`, the consent was revoked or expired (for example
when the app is still in "Testing" mode, or the password changed). Redo the consent:
`auth-url`, open it, allow, then `exchange` with the address the browser ends up on.

## Release path

Since 7A.8 an image reaches the Monday job only through `deploy/release.sh`, and only after a
cloud dry run succeeded. Do not run `gcloud builds submit` plus `run jobs update` by hand any more.

```
merge to main -> build image <commit> -> smoke test -> push -> dry run (hari-lari-weekly-dryrun)
              -> only if it succeeded: point hari-lari-weekly at <commit>
              -> sync the scheduler job hari-lari-tick with the tick times of <commit>
```

* **Automatic:** `.github/workflows/release.yml` runs the script on every push to `main`. It
  signs in to Google Cloud with Workload Identity Federation (no key file) as the service
  account `hari-lari-release`, which may only push to the registry, update and run the two jobs,
  and act as `hari-lari-job`. Only tokens of this repository on the branch `main` are accepted.
* **The image tag is the first 12 characters of the commit.** The live job keeps the tag it ran
  before as the label `previous-release`.
* **The dry run** is the job `hari-lari-weekly-dryrun`: the same configuration as the live job,
  always started with `tick --dry-run --all-scheduled` (the script sets the arguments on every
  release), so every scheduled report must build, and it sends nothing, writes under `dry-run/`
  and has no scheduler. If it fails, the live job still runs the old image and the workflow
  fails; read the execution log in Cloud Run.
* **The scheduler jobs** follow the promoted image. An image with the tick
  (`hari-lari-pipeline schedule --json`) gets the one job `hari-lari-tick`, `0 4,6 * * *`
  WITA, started with `tick --scheduler`, and the `hari-lari-report-*` jobs are deleted. An image
  from before the tick gets its `hari-lari-report-<id>` jobs back and `hari-lari-tick` is
  deleted. Other scheduler jobs are left alone. By hand: `deploy/release.sh schedules [tag]`.
* **Rolling back:** Actions > Release > Run workflow > `rollback`, or by hand
  `deploy/release.sh rollback` (previous release) or `deploy/release.sh rollback <tag>`. A
  rollback also syncs the scheduler jobs of the image it goes back to, so it needs `docker`.
* **Do not roll back across the cut-over (9.7):** an image older than `dc1499cb5270` runs the old
  command and does not understand the scheduler's `run --report weekly`.
* **By hand:** `deploy/release.sh {build|dryrun|promote|rollback|schedules|all|status} [tag]`; without a tag
  it uses the current commit, which must be committed (`ALLOW_DIRTY=1` overrides). `status` shows
  the image of both jobs and the previous release. Needs `docker` and a `gcloud` login.
* **A release that is promoted but wrong on a Monday:** roll back, then re-run the week with
  `--force` if needed (see "Re-running a day").
* **Registry clean-up:** a policy keeps `latest` and the 20 most recent versions and deletes
  anything older than 60 days. The live image and its rollback target are always recent,
  because only a release that passed its dry run is promoted.
* **Cost:** Cloud Build is not used. The build runs on a GitHub runner: about 10 minutes per
  release against 2,000 free minutes a month for a private repository. Artifact Registry
  charges from 0.5 GB stored and is the largest item; see "Cost hygiene". Check the bill after
  the first month on the tick.

### Cost hygiene (plan t18, checked 27 Nov 2026)

List prices, not the bill; the bill is read after the first month on the tick (about 1 Jan 2027)
and written into the plan's "Cost budget".

| Where | State on 27 Nov | Rule |
| --- | --- | --- |
| Artifact Registry `hari-lari` | 5.5 GB, about 30 versions after five days of releases (about three versions per tag); 0.5 GB is free, so about 0.5 USD a month at 0.10 USD per GB | The policy keeps `latest` and the 20 most recent versions (about seven releases, the rollback target included) and deletes anything older than 60 days. The 60-day rule deletes nothing yet. If the bill shows the size matters, lower `keepCount` in `deploy/setup.sh cleanup-policy`, never below the live image, the previous release and a spare |
| Cloud Storage `hari-lari-data` | four lifecycle rules: `raw/` to Nearline at 90 days, `landing/` `*.json.gz` deleted at 90 days, `warehouse/2*` deleted at 30 days, `dry-run/` deleted at 14 days | Nothing else expires: the Parquet landing, `reports/`, `runs/` and `config/` grow by the day's tables and records only |
| Secret Manager | 13 secrets, so at least 13 active versions if each holds one (only `moka-oauth-token` was listed: one): about 7 above the free six, about 0.4 USD a month | Google bills a version as active when it is **enabled or disabled**; only a destroyed version is free (pricing page, checked 27 Nov), so disabling an old version saves nothing. After adding a version by hand, destroy the one it replaces if nothing needs it: `gcloud secrets versions destroy <number> --secret <name>`. That cannot be undone. Each extra version costs 0.06 USD a month |
| Cloud Scheduler | two jobs, `hari-lari-tick` and `expense-bot-tick`; three are free per billing account | A third job (a second report schedule) fits; a fourth is billed. Reports share the one tick for that reason |

### One-time setup (you run it, with an owner login in `gcloud`)

```bash
deploy/setup.sh apis          # also enables iamcredentials, needed for the token exchange
deploy/setup.sh dryrun-job    # the job hari-lari-weekly-dryrun, with the live job's image
deploy/setup.sh cleanup-policy
deploy/setup.sh wif           # pool, provider, release service account, roles
deploy/setup.sh report-scheduler   # 9.7: scheduler jobs per report, run with arguments
```

Each step is safe to re-run. `GITHUB_REPO=owner/name` overrides the repository the provider
trusts. `wif` prints two values: add them in GitHub under Settings > Secrets and variables >
Actions > **Variables** as `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_RELEASE_SERVICE_ACCOUNT`.
Then push to `main`, or run the workflow by hand, and check that the dry run and the promotion
went through (`deploy/release.sh status`).

Note: `deploy/setup.sh job` no longer resets the image of an existing job to `latest`; the
release path owns the image from now on.
