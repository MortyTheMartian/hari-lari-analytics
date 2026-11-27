# Settings shared by deploy/setup.sh (one-time infrastructure) and deploy/release.sh (images).
# Source it; it defines variables and the helper `g`, and runs nothing.

PROJECT="${PROJECT:-hari-lari}"
REGION="${REGION:-asia-southeast2}"
BUCKET="${BUCKET:-${PROJECT}-data}"
CONFIG_URL="gs://${BUCKET}/config"   # the private business config (CONFIG_DIR of the jobs)
REPO="hari-lari"
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/weekly"
JOB="hari-lari-weekly"                  # the live job, started by Cloud Scheduler
DRYRUN_JOB="hari-lari-weekly-dryrun"    # same configuration, runs with --dry-run, never scheduled
INVOKER_SA="hari-lari-scheduler@${PROJECT}.iam.gserviceaccount.com"  # Cloud Scheduler's account
TICK_JOB="hari-lari-tick"               # the one scheduler job: the daily tick (D22, D27)
SCHEDULE_PREFIX="hari-lari-report-"     # the per-report scheduler jobs of images before the tick

g() { gcloud --quiet --project "$PROJECT" "$@"; }
