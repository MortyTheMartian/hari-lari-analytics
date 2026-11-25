# Settings shared by deploy/setup.sh (one-time infrastructure) and deploy/release.sh (images).
# Source it; it defines variables and the helper `g`, and runs nothing.

PROJECT="${PROJECT:-hari-lari}"
REGION="${REGION:-asia-southeast2}"
BUCKET="${BUCKET:-${PROJECT}-data}"
REPO="hari-lari"
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/weekly"
JOB="hari-lari-weekly"                  # the live job, started by Cloud Scheduler
DRYRUN_JOB="hari-lari-weekly-dryrun"    # same configuration, runs with --dry-run, never scheduled
INVOKER_SA="hari-lari-scheduler@${PROJECT}.iam.gserviceaccount.com"  # Cloud Scheduler's account
SCHEDULE_PREFIX="hari-lari-report-"     # one scheduler job per report with a schedule

g() { gcloud --quiet --project "$PROJECT" "$@"; }
