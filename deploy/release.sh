#!/usr/bin/env bash
# Phase 7A.8: the release path of the weekly job. One implementation, used by
# .github/workflows/release.yml and by hand (see docs/runbook.md, "Release path").
#
#   deploy/release.sh tag               print the tag of HEAD (its first 12 characters)
#   deploy/release.sh build [tag]       docker build, smoke test, push <image>:<tag>
#   deploy/release.sh dryrun [tag]      run the image as hari-lari-weekly-dryrun; fails if the run fails
#   deploy/release.sh promote [tag]     point the live job at the tag, remembering the previous one
#   deploy/release.sh rollback [tag]    point the live job at the tag (default: the previous release)
#   deploy/release.sh all [tag]         build, dryrun, promote; stops at the first failure
#   deploy/release.sh status            the image of each job and the previous release
#
# Without a tag, the tag is the current commit, which must have no uncommitted changes
# (ALLOW_DIRTY=1 overrides). A tag is lower case letters, digits, "_" and "-", because the
# live job also keeps it as a label.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$ROOT/deploy/common.sh"

die() { echo "release.sh: $*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }

head_tag() { git -C "$ROOT" rev-parse --short=12 HEAD; }

# The tag to use: the argument, or the commit. A tag taken from the commit means "this commit".
resolve_tag() { # [tag]
  local tag="${1:-}"
  if [[ -z "$tag" ]]; then
    if [[ -z "${ALLOW_DIRTY:-}" && -n "$(git -C "$ROOT" status --porcelain)" ]]; then
      die "uncommitted changes: the image would not be the commit. Commit, name a tag, or set ALLOW_DIRTY=1"
    fi
    tag="$(head_tag)"
  fi
  [[ "$tag" =~ ^[a-z0-9_][a-z0-9_-]{0,62}$ ]] || die "not a valid tag: $tag"
  printf %s "$tag"
}

describe_job() { g run jobs describe "$1" --region "$REGION" --format json; }

# The describe output has the Knative shape (spec.template...) or the v2 shape (template...),
# depending on the gcloud version; read both.
job_image() { # job
  describe_job "$1" | python3 -c '
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
'
}

job_label() { # job key
  describe_job "$1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for labels in (d.get("metadata", {}).get("labels"), d.get("labels")):
    if labels and sys.argv[1] in labels:
        print(labels[sys.argv[1]])
        break
' "$2"
}

# The tag part of the image a job runs now; empty when it runs a digest.
job_tag() { # job
  local image
  image="$(job_image "$1")"
  image="${image%@*}"
  if [[ "${image##*/}" == *:* ]]; then
    printf %s "${image##*:}"
  fi
}

step_build() { # [tag]
  local tag image
  tag="$(resolve_tag "${1:-}")"
  image="$IMAGE_REPO:$tag"
  g auth configure-docker "${REGION}-docker.pkg.dev"
  docker build --platform linux/amd64 --tag "$image" "$ROOT"
  # The image must start and load its components before it goes anywhere
  docker run --rm --entrypoint hari-lari-component "$image" validate
  docker push "$image"
  echo "built and pushed $image"
}

step_dryrun() { # [tag]
  local tag
  tag="$(resolve_tag "${1:-}")"
  # --args is set every time, so a job whose arguments were cleared can never send a report
  g run jobs update "$DRYRUN_JOB" --region "$REGION" --image "$IMAGE_REPO:$tag" --args=--dry-run
  if ! g run jobs execute "$DRYRUN_JOB" --region "$REGION" --wait; then
    die "the dry run of $tag failed; the live job still runs $(job_tag "$JOB")"
  fi
  echo "dry run of $tag succeeded"
}

step_promote() { # [tag]
  local tag current labels
  tag="$(resolve_tag "${1:-}")"
  g artifacts docker images describe "$IMAGE_REPO:$tag" >/dev/null ||
    die "there is no image $IMAGE_REPO:$tag"
  current="$(job_tag "$JOB")"
  labels="release=$tag"
  if [[ -n "$current" && "$current" != "$tag" ]]; then
    labels="$labels,previous-release=$current"
  fi
  g run jobs update "$JOB" --region "$REGION" --image "$IMAGE_REPO:$tag" --update-labels "$labels"
  echo "live job $JOB: ${current:-?} -> $tag"
}

step_rollback() { # [tag]
  local tag="${1:-}"
  if [[ -z "$tag" ]]; then
    tag="$(job_label "$JOB" previous-release)"
  fi
  [[ -n "$tag" ]] || die "no previous release is recorded; name the tag: deploy/release.sh rollback <tag>"
  step_promote "$tag"
}

step_all() { # [tag]
  local tag
  tag="$(resolve_tag "${1:-}")"
  step_build "$tag"
  step_dryrun "$tag"
  step_promote "$tag"
}

step_status() {
  local previous
  printf '%-26s %s\n' "$JOB" "$(job_image "$JOB")"
  printf '%-26s %s\n' "$DRYRUN_JOB" "$(job_image "$DRYRUN_JOB")"
  previous="$(job_label "$JOB" previous-release)"
  printf '%-26s %s\n' "previous release" "${previous:-none recorded}"
}

case "${1:-}" in
  tag) head_tag ;;
  build|dryrun|promote|rollback|all) "step_$1" "${2:-}" ;;
  status) step_status ;;
  *) usage; exit 1 ;;
esac
