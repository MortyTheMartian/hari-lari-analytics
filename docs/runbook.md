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
3. Re-send the missed report: `hari-lari-weekly --anchor <the Sunday> --force` (see "The weekly job").

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

## The weekly job (`hari-lari-weekly`)

One command does everything: extract the Moka API, build the report, store the files, upload to
Drive, send to Telegram. Cloud Scheduler starts it every Monday at 04:00 WITA.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Everything except sending: no Telegram message (also no alert), no Drive upload, no marker. Files go under `dry-run/` in the data root. |
| `--to admin` | A private run: the report goes to the admin chat. Not marked as sent, so it never blocks the real send. |
| `--anchor YYYY-MM-DD` | Last day of the report (default: yesterday in Bali). |
| `--force` | Send even if this week was already sent. |
| `--data-root` | Local folder or `gs://bucket`; env `DATA_ROOT`. |

Files in the data root:

| Path | What |
| --- | --- |
| `raw/<run date>/` | The extraction: Parquet tables, `manifest.json`, raw JSON |
| `reports/<anchor>/` | The PDF, the cover PNG, the Excel lines |
| `sent/<anchor>.json` | The sent-marker. Its existence means "this week went to the group" |
| `dry-run/...` | The same layout, written by dry runs |

### Running it by hand

```bash
# locally (reads the token from extraction/moka/.token.json, secrets through gcloud)
uv run --extra gcp hari-lari-weekly --dry-run --data-root /tmp/hl
# in the cloud
gcloud run jobs execute hari-lari-weekly --region asia-southeast2 --project hari-lari \
  --args=--dry-run              # or --args=--to,admin   or --args=--anchor,2026-11-22,--force
```

Logs are JSON lines. In Cloud Logging, filter by step:
`resource.type="cloud_run_job" jsonPayload.step="deliver"`.

### Re-running a week

* **It says "already sent":** the marker exists. Use `--force` only if you want the group to get
  the report a second time.
* **Cloud Run retried after a failure:** nothing was sent twice. The marker is written only after
  a successful send; a failed run sends the admin alert and leaves no marker.
* **The send failed but the files are fine:** they are in `reports/<anchor>/`. Re-run with
  `--anchor <Sunday>`; the extraction is repeated (about 3 minutes), the data is complete up to
  yesterday.

### What the admin chat tells you

| Message | Meaning and action |
| --- | --- |
| `Hari Lari weekly report failed at step: X` | Step X (`check`, `extract`, `model`, `report`, `upload`, `deliver`) failed and the run ended with an error. Open the logs link, fix, re-run. Nothing reached the group unless X is after `deliver`. |
| `step drive failed ... without the Drive link` | The Drive upload failed. The group still got the cover and the PDF, just no link. Run `hari-lari-drive whoami`; if it says `invalid_grant`, redo the consent (see Drive below). |
| `step marker failed ... could not be written` | The report was sent, but the "already sent" marker is missing. A plain re-run would send the report again. Create `sent/<anchor>.json` by hand. |
| `The Moka API token expires on ...` | See below. |

### Re-authorising the Moka token

The token is refreshed automatically when fewer than 30 days remain, and the refresh is saved to
Secret Manager. The reminder (fewer than 45 days left) only shows up when that did not extend it.
To start from scratch:

```bash
uv run --extra gcp hari-lari-extract auth url          # open the printed link, allow, copy the redirect URL
uv run --extra gcp hari-lari-extract --token-store secret --gcp-project hari-lari auth exchange "<redirect URL>"
```

The second command adds a new version of the secret `moka-oauth-token`; the next run uses it.

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
