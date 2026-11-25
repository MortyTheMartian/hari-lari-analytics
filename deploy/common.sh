# Settings shared by deploy/setup.sh (one-time infrastructure) and deploy/release.sh (images).
# Source it; it defines variables and the helper `g`, and runs nothing.

PROJECT="${PROJECT:-hari-lari}"
REGION="${REGION:-asia-southeast2}"
BUCKET="${BUCKET:-${PROJECT}-data}"
REPO="hari-lari"
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/weekly"
JOB="hari-lari-weekly"                  # the live job, started by Cloud Scheduler
DRYRUN_JOB="hari-lari-weekly-dryrun"    # same configuration, runs with --dry-run, never scheduled

g() { gcloud --quiet --project "$PROJECT" "$@"; }
