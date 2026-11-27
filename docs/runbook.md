# Runbook: Hari Lari weekly report

Status: started in phase 4 (Telegram). Phase 6.6 completes it (re-running a week, re-authorising
the Moka token, adding members, what each alert means).

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
3. Re-send the missed report: `hari-lari-report run --report weekly --anchor <the Sunday> --force`
   (see "The report job").

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

## The report job (`hari-lari-report run`)

One command runs any report of `src/hari_lari_analytics/presentation/reports/`: extract the Moka
API, build the models and run the data checks, build the report, store the files, upload to
Drive, send to Telegram and write the sent-marker. All reports share the Cloud Run job
`hari-lari-weekly`; each report with a `schedule` has a Cloud Scheduler job
`hari-lari-report-<id>` that starts it with `run --report <id>`. The weekly report's runs every
Monday at 04:00 WITA. The release path creates, updates and deletes these scheduler jobs from
the reports' YAML (see "Release path"), so a schedule change goes live with a release.

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
# in the cloud
gcloud run jobs execute hari-lari-weekly --region asia-southeast2 --project hari-lari \
  --args=run,--report,weekly,--dry-run   # or ...,--to,admin   or ...,--anchor,2026-11-29,--force
```

The job has no arguments of its own: started without `--args` it only prints the usage and fails.

Every run writes the run ledger, one JSON record per step under `runs/<run date>/` (dry runs
under `dry-run/runs/`). To see what ran, what was sent to whom and what failed:

```bash
uv run --extra gcp hari-lari-runs list --data-root gs://hari-lari-data --since 2026-11-30
uv run hari-lari-runs list --report weekly          # local: data/runs/
```

Logs are JSON lines. In Cloud Logging, filter by step:
`resource.type="cloud_run_job" jsonPayload.step="deliver"`.

### Re-running a week

* **It says "already sent":** the marker exists. Use `--force` only if you want the group to get
  the report a second time.
* **Cloud Run retried after a failure:** nothing was sent twice. The marker is written only after
  a successful send; a failed run sends the admin alert and leaves no marker.
* **The send failed but the files are fine:** they are in `reports/<report>/<anchor>/`. Re-run
  with `--anchor <Sunday>`; the extraction is repeated (about 3 minutes), the data is complete up
  to yesterday.

### What the admin chat tells you

| Message | Meaning and action |
| --- | --- |
| `Hari Lari report <id> failed at step: X` | Step X (`check`, `extract`, `model`, `report`, `upload`, `deliver`) failed and the run ended with an error. `model` includes the data checks (`checks.yml`): an error-level check fails the run and names the failing rows. Open the logs link, fix, re-run. Nothing reached the group unless X is after `deliver`. |
| `step drive failed ... without the Drive link` | The Drive upload failed. The group still got the cover and the PDF, just no link. Run `hari-lari-drive whoami`; if it says `invalid_grant`, redo the consent (see Drive below). |
| `step marker failed ... could not be written` | The report was sent and the ledger says so, so a re-run does not send it again; only an image from before the ledger (after a rollback) would. Create `sent/<report>/<anchor>.json` by hand before such a rollback. |
| `the ledger record ... could not be written` | The step ran, but its record is missing. If it was a report that went out, a re-run would send it again unless its sent-marker exists (`sent/<report>/<anchor>.json`). |
| `The Moka API token expires on ...` | See below. |

### The cut-over (plan 9.7, done 25 Nov 2026)

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
              -> sync the scheduler jobs hari-lari-report-<id> with the reports of <commit>
```

* **Automatic:** `.github/workflows/release.yml` runs the script on every push to `main`. It
  signs in to Google Cloud with Workload Identity Federation (no key file) as the service
  account `hari-lari-release`, which may only push to the registry, update and run the two jobs,
  and act as `hari-lari-job`. Only tokens of this repository on the branch `main` are accepted.
* **The image tag is the first 12 characters of the commit.** The live job keeps the tag it ran
  before as the label `previous-release`.
* **The dry run** is the job `hari-lari-weekly-dryrun`: the same configuration as the live job,
  always started with `run --scheduled --dry-run` (the script sets the arguments on every
  release), so every scheduled report must build, and it sends nothing, writes under `dry-run/`
  and has no scheduler. If it fails, the live job still runs the old image and the workflow
  fails; read the execution log in Cloud Run.
* **The scheduler jobs** follow the reports of the promoted image (`hari-lari-report schedules
  --json`): one `hari-lari-report-<id>` per report with a schedule, paused when `enabled: false`,
  deleted when the report loses its schedule. Other scheduler jobs are left alone. By hand:
  `deploy/release.sh schedules [tag]`.
* **Rolling back:** Actions > Release > Run workflow > `rollback`, or by hand
  `deploy/release.sh rollback` (previous release) or `deploy/release.sh rollback <tag>`.
* **Do not roll back across the cut-over (9.7):** an image older than `dc1499cb5270` runs the old
  command and does not understand the scheduler's `run --report weekly`.
* **By hand:** `deploy/release.sh {build|dryrun|promote|rollback|schedules|all|status} [tag]`; without a tag
  it uses the current commit, which must be committed (`ALLOW_DIRTY=1` overrides). `status` shows
  the image of both jobs and the previous release. Needs `docker` and a `gcloud` login.
* **A release that is promoted but wrong on a Monday:** roll back, then re-run the week with
  `--force` if needed (see "Re-running a week").
* **Registry clean-up:** a policy keeps `latest` and the 20 most recent versions and deletes
  anything older than 60 days. The live image and its rollback target are always recent,
  because only a release that passed its dry run is promoted.
* **Cost:** Cloud Build is not used. The build runs on a GitHub runner: about 10 minutes per
  release against 2,000 free minutes a month for a private repository. Artifact Registry
  charges from 0.5 GB stored; the clean-up policy limits that. Check the bill after the first
  month.

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
