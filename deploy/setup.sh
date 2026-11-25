#!/usr/bin/env bash
# Phase 3: Google Cloud infrastructure for the weekly Hari Lari job. Idempotent: safe to re-run.
# Usage: deploy/setup.sh <step>   (steps: apis bucket sa secrets registry build job scheduler alert all)
# Phase 4 (not part of `all`): deploy/setup.sh telegram-token | telegram-chat group|admin <id>
# Phase 5 (not part of `all`): deploy/setup.sh drive-secrets   (OAuth client for the Drive upload)
#   then DRIVE_FOLDER_ID=<folder id> deploy/setup.sh job
# Phase 7A.8 (not part of `all`; run once, in this order, after `job`):
#   deploy/setup.sh dryrun-job | cleanup-policy | wif    (see docs/runbook.md, "Release path")
# Manual (console): billing budget (3.4), notification channel email check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$ROOT/deploy/common.sh"   # PROJECT, REGION, BUCKET, REPO, JOB, DRYRUN_JOB, IMAGE_REPO, g
JOB_SA_NAME="hari-lari-job"
JOB_SA="${JOB_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA_NAME="hari-lari-scheduler"
INVOKER_SA="${INVOKER_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${IMAGE_REPO}:latest"
ALERT_EMAIL="${ALERT_EMAIL:-harilari.bali@gmail.com}"
TOKEN_FILE="${TOKEN_FILE:-$ROOT/extraction/moka/.token.json}"
# 7A.8: the GitHub repository whose workflow may release, and the identity pool for it
GITHUB_REPO="${GITHUB_REPO:-MortyTheMartian/hari-lari-analytics}"
WIF_POOL="github"
WIF_PROVIDER="github-actions"
RELEASE_SA_NAME="hari-lari-release"
RELEASE_SA="${RELEASE_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

step_apis() { # 3.3
  g services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com storage.googleapis.com \
    logging.googleapis.com monitoring.googleapis.com drive.googleapis.com \
    iamcredentials.googleapis.com
}

step_bucket() { # 3.5
  g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || g storage buckets create "gs://$BUCKET" \
    --location "$REGION" --uniform-bucket-level-access --public-access-prevention
  local lc; lc="$(mktemp)"
  cat >"$lc" <<'JSON'
{"rule": [{"action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
           "condition": {"age": 90, "matchesPrefix": ["raw/"]}}]}
JSON
  g storage buckets update "gs://$BUCKET" --lifecycle-file "$lc"
  rm -f "$lc"
}

step_sa() { # 3.6 (+ invoker for 3.12)
  g iam service-accounts describe "$JOB_SA" >/dev/null 2>&1 ||
    g iam service-accounts create "$JOB_SA_NAME" --display-name "Hari Lari weekly job"
  g iam service-accounts describe "$INVOKER_SA" >/dev/null 2>&1 ||
    g iam service-accounts create "$INVOKER_SA_NAME" --display-name "Hari Lari scheduler invoker"
  g storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$JOB_SA" --role roles/storage.objectAdmin >/dev/null
  # Secret-level grants happen in step_secrets (after the secrets exist)
}

ensure_secret() { # name
  g secrets describe "$1" >/dev/null 2>&1 || g secrets create "$1" --replication-policy automatic
}

grant_secret() { # name role
  g secrets add-iam-policy-binding "$1" --member "serviceAccount:$JOB_SA" --role "$2" >/dev/null
}

# 3.7 Moka secrets from local files (values never printed). Telegram secrets: phase 4.
step_secrets() {
  # shellcheck disable=SC1091
  set -a; source "$ROOT/.env"; set +a
  for s in moka-client-id moka-client-secret moka-oauth-token; do ensure_secret "$s"; done
  printf %s "$MOKA_CLIENT_ID" | g secrets versions add moka-client-id --data-file=-
  printf %s "$MOKA_CLIENT_SECRET" | g secrets versions add moka-client-secret --data-file=-
  g secrets versions add moka-oauth-token --data-file="$TOKEN_FILE"
  grant_secret moka-client-id roles/secretmanager.secretAccessor
  grant_secret moka-client-secret roles/secretmanager.secretAccessor
  grant_secret moka-oauth-token roles/secretmanager.secretAccessor
  grant_secret moka-oauth-token roles/secretmanager.secretVersionAdder
  # Telegram secrets: create empty shells now so the job definition is stable; values in phase 4
  for s in telegram-bot-token telegram-chat-id telegram-admin-chat-id; do
    ensure_secret "$s"; grant_secret "$s" roles/secretmanager.secretAccessor
  done
}

# 4.1 / 4.5 Telegram values. The bot token is typed hidden and never echoed or stored in a file.
step_telegram_token() {
  local token
  read -rsp "Paste the bot token from @BotFather (input is hidden), then press Enter: " token
  echo
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || { echo "That does not look like a bot token."; exit 1; }
  ensure_secret telegram-bot-token
  printf %s "$token" | g secrets versions add telegram-bot-token --data-file=-
}

step_telegram_chat() { # group|admin <chat id>
  local name="$1" id="${2:-}"
  [[ "$name" == group || "$name" == admin ]] || { echo "usage: $0 telegram-chat group|admin <chat id>"; exit 1; }
  [[ "$id" =~ ^-?[0-9]+$ ]] || { echo "The chat id must be a number such as -1001234567890 or 123456789."; exit 1; }
  local secret=telegram-chat-id
  [[ "$name" == admin ]] && secret=telegram-admin-chat-id
  ensure_secret "$secret"
  printf %s "$id" | g secrets versions add "$secret" --data-file=-
}

step_registry() { # 3.8
  g artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
    g artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
      --description "Hari Lari images"
}

step_build() { # 3.10
  g builds submit "$ROOT" --region "$REGION" --tag "$IMAGE" --gcs-source-staging-dir "gs://$BUCKET/_build"
}

SECRETS="MOKA_CLIENT_ID=moka-client-id:latest,MOKA_CLIENT_SECRET=moka-client-secret:latest,TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest,TELEGRAM_ADMIN_CHAT_ID=telegram-admin-chat-id:latest,DRIVE_OAUTH_CLIENT_ID=drive-oauth-client-id:latest,DRIVE_OAUTH_CLIENT_SECRET=drive-oauth-client-secret:latest,DRIVE_OAUTH_REFRESH_TOKEN=drive-oauth-refresh-token:latest"

job_image_of() { # job: the image a job runs now (empty when it does not exist)
  g run jobs describe "$1" --region "$REGION" --format json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
for path in (("spec", "template", "spec", "template", "spec", "containers"),
             ("template", "template", "containers")):
    node = d
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if node:
        print(node[0]["image"])
        break
' || true
}

# One definition for the live job and its dry-run twin: same service account, size, variables
# and secrets. Arguments: job name, image for a new job, then extra gcloud flags. An existing
# job keeps its image, so re-running setup never undoes a release (deploy/release.sh owns it).
deploy_job() { # name image [flags...]
  local name="$1" image="$2"; shift 2
  local env="TZ=Asia/Makassar,TOKEN_STORE=secret,GCP_PROJECT=$PROJECT,EXTRACT_OUT=gs://$BUCKET/raw,DATA_ROOT=gs://$BUCKET"
  [[ -n "${DRIVE_FOLDER_ID:-}" ]] && env="$env,DRIVE_FOLDER_ID=$DRIVE_FOLDER_ID"
  local verb=create envflag=--set-env-vars imageflag=(--image "$image")
  if g run jobs describe "$name" --region "$REGION" >/dev/null 2>&1; then
    # an update keeps the variables it is not given (DRIVE_FOLDER_ID)
    verb=update envflag=--update-env-vars imageflag=()
  fi
  g run jobs "$verb" "$name" --region "$REGION" "${imageflag[@]}" \
    --service-account "$JOB_SA" \
    --cpu 1 --memory 1Gi --task-timeout 20m --max-retries 1 --tasks 1 \
    "$envflag" "$env" --set-secrets "$SECRETS" "$@"
}

step_job() { # 3.11, 5.x: the weekly job with its Telegram secrets and Drive folder
  # Roles the build/runtime default accounts need are granted by the service-account step.
  deploy_job "$JOB" "$IMAGE"
}

# 7A.8 The dry-run job: the live job's configuration, run with --dry-run, never scheduled. The
# release script sets its image and its argument on every release. It is created with the image
# of the live job.
step_dryrun_job() {
  local image
  image="$(job_image_of "$JOB")"
  deploy_job "$DRYRUN_JOB" "${image:-$IMAGE}" --args=--dry-run
}

# 7A.8 Clean-up policy of the registry. Every merge to main pushes an image, so old ones go:
# keep `latest`, keep the 20 most recent versions, delete the rest once older than 60 days. The
# live job and its rollback target are among the most recent versions, because only an image
# that passed its dry run is promoted; check `deploy/release.sh status` before you push more
# than 20 images that are not promoted.
step_cleanup_policy() {
  local pol; pol="$(mktemp --suffix .json)"
  cat >"$pol" <<'JSON'
[
  {"name": "keep-latest-tag", "action": {"type": "Keep"},
   "condition": {"tagState": "tagged", "tagPrefixes": ["latest"]}},
  {"name": "keep-recent", "action": {"type": "Keep"},
   "mostRecentVersions": {"keepCount": 20}},
  {"name": "delete-old", "action": {"type": "Delete"},
   "condition": {"olderThan": "5184000s"}}
]
JSON
  g artifacts repositories set-cleanup-policies "$REPO" --location "$REGION" \
    --policy "$pol" --no-dry-run
  rm -f "$pol"
}

# 7A.8 Workload Identity Federation for GitHub Actions: no key file. The workflow exchanges its
# GitHub token for the short-lived identity of the release service account, which may only push
# images, update and run the two jobs, and act as the job's service account. The provider
# accepts tokens of one repository and one branch (main) only. Run `dryrun-job` first.
step_wif() {
  local number principal job
  number="$(g projects describe "$PROJECT" --format 'value(projectNumber)')"

  g iam service-accounts describe "$RELEASE_SA" >/dev/null 2>&1 ||
    g iam service-accounts create "$RELEASE_SA_NAME" --display-name "Hari Lari release (GitHub Actions)"

  g iam workload-identity-pools describe "$WIF_POOL" --location global >/dev/null 2>&1 ||
    g iam workload-identity-pools create "$WIF_POOL" --location global --display-name "GitHub"
  local condition="assertion.repository == '$GITHUB_REPO' && assertion.ref == 'refs/heads/main'"
  local flags=(--location global --workload-identity-pool "$WIF_POOL"
    --issuer-uri "https://token.actions.githubusercontent.com"
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
    --attribute-condition "$condition")
  if g iam workload-identity-pools providers describe "$WIF_PROVIDER" \
    --location global --workload-identity-pool "$WIF_POOL" >/dev/null 2>&1; then
    g iam workload-identity-pools providers update-oidc "$WIF_PROVIDER" "${flags[@]}"
  else
    g iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" "${flags[@]}" \
      --display-name "GitHub Actions"
  fi

  # Only tokens of this repository may act as the release service account
  principal="principalSet://iam.googleapis.com/projects/${number}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}"
  g iam service-accounts add-iam-policy-binding "$RELEASE_SA" \
    --member "$principal" --role roles/iam.workloadIdentityUser >/dev/null

  # Least privilege: push to this registry, change and run the two jobs, act as the job account
  g artifacts repositories add-iam-policy-binding "$REPO" --location "$REGION" \
    --member "serviceAccount:$RELEASE_SA" --role roles/artifactregistry.writer >/dev/null
  for job in "$JOB" "$DRYRUN_JOB"; do
    g run jobs add-iam-policy-binding "$job" --region "$REGION" \
      --member "serviceAccount:$RELEASE_SA" --role roles/run.developer >/dev/null
  done
  g iam service-accounts add-iam-policy-binding "$JOB_SA" \
    --member "serviceAccount:$RELEASE_SA" --role roles/iam.serviceAccountUser >/dev/null

  cat <<EOF

Done. In GitHub, repository $GITHUB_REPO > Settings > Secrets and variables > Actions >
Variables, add these two repository variables (they are not secrets):

  GCP_WORKLOAD_IDENTITY_PROVIDER = projects/${number}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}
  GCP_RELEASE_SERVICE_ACCOUNT    = ${RELEASE_SA}
EOF
}

# 5.1 Drive upload. A service account cannot create files in a normal Drive (no storage quota),
# so the job uploads as the Google user who gave consent once (`hari-lari-drive`). Run this step,
# then `uv run --extra gcp hari-lari-drive auth-url` and `... exchange "<URL>"`.
step_drive_secrets() {
  local id secret
  read -rp "OAuth client ID (ends in .apps.googleusercontent.com): " id
  read -rsp "OAuth client secret (input is hidden): " secret
  echo
  [[ "$id" == *.apps.googleusercontent.com && -n "$secret" ]] || { echo "That does not look like an OAuth client."; exit 1; }
  for s in drive-oauth-client-id drive-oauth-client-secret drive-oauth-refresh-token; do
    ensure_secret "$s"; grant_secret "$s" roles/secretmanager.secretAccessor
  done
  printf %s "$id" | g secrets versions add drive-oauth-client-id --data-file=-
  printf %s "$secret" | g secrets versions add drive-oauth-client-secret --data-file=-
}

step_scheduler() { # 3.12
  g run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
    --member "serviceAccount:$INVOKER_SA" --role roles/run.invoker >/dev/null
  local uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run"
  local verb=create
  g scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1 && verb=update
  g scheduler jobs "$verb" http "$JOB" --location "$REGION" \
    --schedule "0 4 * * 1" --time-zone "Asia/Makassar" \
    --uri "$uri" --http-method POST --oauth-service-account-email "$INVOKER_SA"
  # Stay paused until go-live (phase 6.4)
  g scheduler jobs pause "$JOB" --location "$REGION"
}

step_alert() { # 3.13
  local ch
  ch="$(g beta monitoring channels list --filter "displayName='Hari Lari admin email'" --format 'value(name)' | head -1)"
  if [[ -z "$ch" ]]; then
    ch="$(g beta monitoring channels create --display-name 'Hari Lari admin email' \
      --type email --channel-labels "email_address=$ALERT_EMAIL" --format 'value(name)')"
  fi
  if [[ -z "$(g alpha monitoring policies list --filter "displayName='Hari Lari job failed'" --format 'value(name)')" ]]; then
    local pol; pol="$(mktemp --suffix .json)"
    cat >"$pol" <<JSON
{
  "displayName": "Hari Lari job failed",
  "combiner": "OR",
  "conditions": [{
    "displayName": "Cloud Run job ERROR log",
    "conditionMatchedLog": {
      "filter": "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$JOB\" AND severity>=ERROR"
    }
  }],
  "alertStrategy": {"notificationRateLimit": {"period": "3600s"}, "autoClose": "604800s"},
  "notificationChannels": ["$ch"]
}
JSON
    g alpha monitoring policies create --policy-from-file "$pol"
    rm -f "$pol"
  fi
}

case "${1:-}" in
  apis|bucket|sa|secrets|registry|build|job|scheduler|alert)
    "step_$1" ;;
  drive-secrets) step_drive_secrets ;;
  dryrun-job) step_dryrun_job ;;
  cleanup-policy) step_cleanup_policy ;;
  wif) step_wif ;;
  telegram-token) step_telegram_token ;;
  telegram-chat) step_telegram_chat "${2:-}" "${3:-}" ;;
  all) for s in apis bucket sa secrets registry build job scheduler alert; do echo "== $s"; "step_$s"; done ;;
  *) echo "usage: $0 {apis|bucket|sa|secrets|registry|build|job|scheduler|alert|drive-secrets|dryrun-job|cleanup-policy|wif|all|telegram-token|telegram-chat}"; exit 1 ;;
esac
