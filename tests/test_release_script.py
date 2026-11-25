"""deploy/release.sh against a fake gcloud and a fake docker (nothing touches Google Cloud).

The script is copied into a throw-away git repository, so its commit, its clean-tree rule and
the docker build context are the ones of that repository. Each fake program appends its
arguments to a log, which the tests read back.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
IMAGES = "asia-southeast2-docker.pkg.dev/hari-lari/hari-lari/weekly"
GCLOUD_PREFIX = "gcloud --quiet --project hari-lari "

FAKE_GCLOUD = """#!/usr/bin/env bash
all="$*"
echo "gcloud $all" >> "$FAKE_LOG"
case "$all" in
  *"run jobs describe "*)
    job="${all#*run jobs describe }"; job="${job%% *}"
    cat "$FAKE_DIR/$job.json" ;;
  *"run jobs execute"*) exit "${FAKE_EXECUTE_EXIT:-0}" ;;
  *"artifacts docker images describe"*) [[ -e "$FAKE_DIR/no-image" ]] && exit 1; echo "{}" ;;
  *"scheduler jobs list"*) cat "$FAKE_DIR/schedulers" 2>/dev/null ;;
  *"scheduler jobs describe "*)
    job="${all#*scheduler jobs describe }"; job="${job%% *}"
    cat "$FAKE_DIR/state-$job" 2>/dev/null || echo ENABLED ;;
esac
exit 0
"""

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
[[ "$1" == "${FAKE_DOCKER_FAILS:-}" ]] && exit 1
[[ "$*" == *"schedules --json"* ]] && cat "$FAKE_DIR/schedules.json"
exit 0
"""

WEEKLY_SCHEDULE = [{
    "report": "weekly", "cron": "0 4 * * 1", "timezone": "Asia/Makassar", "enabled": True,
    "args": ["run", "--report", "weekly"],
}]  # fmt: skip


def knative(image: str, **labels: str) -> str:
    """`gcloud run jobs describe --format json` as older gcloud versions print it."""
    container = {"image": image}
    return json.dumps(
        {
            "metadata": {"labels": labels},
            "spec": {"template": {"spec": {"template": {"spec": {"containers": [container]}}}}},
        }
    )


def v2(image: str, **labels: str) -> str:
    """The same job in the shape of the Cloud Run v2 API."""
    template = {"template": {"containers": [{"image": image}]}}
    return json.dumps({"labels": labels, "template": template})


@dataclass
class Release:
    repo: Path
    bin: Path
    fake: Path
    log: Path
    env: dict[str, str] = field(default_factory=dict)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.fake),
            **self.env,
            **env,
        }
        return subprocess.run(
            ["bash", str(self.repo / "deploy" / "release.sh"), *args],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        lines = self.log.read_text().splitlines()
        return [line.removeprefix(GCLOUD_PREFIX) for line in lines]

    def job(self, name: str, text: str) -> None:
        (self.fake / f"{name}.json").write_text(text)

    def head(self) -> str:
        command = ["git", "-C", str(self.repo), "rev-parse", "--short=12", "HEAD"]
        out = subprocess.run(command, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def live_updates(self) -> list[str]:
        """The calls that change the live job (not the dry-run job)."""
        live = "run jobs update hari-lari-weekly --region"
        return [call for call in self.calls() if call.startswith(live)]


@pytest.fixture
def release(tmp_path) -> Release:
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    for name in ("release.sh", "common.sh"):
        shutil.copy(DEPLOY / name, repo / "deploy" / name)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("gcloud", FAKE_GCLOUD), ("docker", FAKE_DOCKER)):
        path = bin_dir / name
        path.write_text(text)
        path.chmod(0o755)
    fake = tmp_path / "fake"
    fake.mkdir()
    result = Release(repo, bin_dir, fake, tmp_path / "calls.log")
    result.job("hari-lari-weekly", knative(f"{IMAGES}:aaa111", release="aaa111"))
    result.job("hari-lari-weekly-dryrun", v2(f"{IMAGES}:aaa111"))
    (fake / "schedules.json").write_text(json.dumps(WEEKLY_SCHEDULE))
    return result


def test_the_scripts_are_valid_bash():
    for name in ("release.sh", "common.sh", "setup.sh"):
        done = subprocess.run(["bash", "-n", str(DEPLOY / name)], capture_output=True, text=True)
        assert done.returncode == 0, f"{name}: {done.stderr}"


def test_the_tag_is_the_commit(release):
    done = release.run("tag")
    assert done.returncode == 0
    assert done.stdout.strip() == release.head()
    assert len(done.stdout.strip()) == 12


def test_build_tags_the_image_with_the_commit_and_smoke_tests_it_before_the_push(release):
    done = release.run("build")
    assert done.returncode == 0, done.stderr
    image = f"{IMAGES}:{release.head()}"
    docker = [c for c in release.log.read_text().splitlines() if c.startswith("docker ")]
    assert docker == [
        f"docker build --platform linux/amd64 --tag {image} {release.repo}",
        f"docker run --rm {image} validate",
        f"docker push {image}",
    ]
    assert release.calls()[0] == "auth configure-docker asia-southeast2-docker.pkg.dev"


@pytest.mark.parametrize("failing", ["build", "run"])
def test_a_failing_build_or_smoke_test_stops_before_the_push(release, failing):
    done = release.run("build", FAKE_DOCKER_FAILS=failing)
    assert done.returncode != 0
    logged = release.log.read_text().splitlines()
    assert any(line.startswith(f"docker {failing}") for line in logged)
    assert not any(line.startswith("docker push") for line in logged)


def test_a_commit_tag_needs_a_clean_tree(release):
    (release.repo / "notes.txt").write_text("not committed")
    done = release.run("build")
    assert done.returncode != 0
    assert "uncommitted changes" in done.stderr
    assert not release.log.exists()
    assert release.run("build", ALLOW_DIRTY="1").returncode == 0
    assert release.run("build", "named-tag").returncode == 0


@pytest.mark.parametrize("tag", ["Upper", "../x", "a;b", "a b", "-x", "x" * 64, "v1.2"])
def test_tags_that_cannot_be_labels_are_refused(release, tag):
    done = release.run("promote", tag)
    assert done.returncode != 0
    assert "not a valid tag" in done.stderr
    assert not release.log.exists()


def test_dryrun_runs_every_scheduled_report_as_a_dry_run_and_waits_for_it(release):
    done = release.run("dryrun", "bbb222")
    assert done.returncode == 0, done.stderr
    assert release.calls() == [
        f"run jobs update hari-lari-weekly-dryrun --region asia-southeast2 "
        f"--image {IMAGES}:bbb222 --args=run,--scheduled,--dry-run",
        "run jobs execute hari-lari-weekly-dryrun --region asia-southeast2 --wait",
    ]


def test_a_failed_dry_run_fails_the_script_and_names_the_live_release(release):
    done = release.run("dryrun", "bbb222", FAKE_EXECUTE_EXIT="1")
    assert done.returncode != 0
    assert "dry run of bbb222 failed" in done.stderr
    assert "still runs aaa111" in done.stderr


def test_promote_points_the_live_job_at_the_tag_and_remembers_the_previous_one(release):
    done = release.run("promote", "bbb222")
    assert done.returncode == 0, done.stderr
    assert release.live_updates() == [
        f"run jobs update hari-lari-weekly --region asia-southeast2 --image {IMAGES}:bbb222 "
        "--update-labels release=bbb222,previous-release=aaa111"
    ]
    assert "aaa111 -> bbb222" in done.stdout


def test_promoting_the_running_tag_keeps_the_recorded_previous_release(release):
    done = release.run("promote", "aaa111")
    assert done.returncode == 0
    assert release.live_updates()[0].endswith("--update-labels release=aaa111")


def test_promote_refuses_a_tag_that_is_not_in_the_registry(release):
    (release.fake / "no-image").write_text("")
    done = release.run("promote", "typo123")
    assert done.returncode != 0
    assert "no image" in done.stderr
    assert release.live_updates() == []


def test_the_live_job_is_only_updated_after_a_successful_dry_run(release):
    done = release.run("all", "bbb222")
    assert done.returncode == 0, done.stderr
    logged = [line.removeprefix(GCLOUD_PREFIX) for line in release.log.read_text().splitlines()]

    def first(prefix: str) -> int:
        return next(i for i, line in enumerate(logged) if line.startswith(prefix))

    steps = [
        first("docker build"),
        first("docker run"),
        first("docker push"),
        first("run jobs update hari-lari-weekly-dryrun"),
        first("run jobs execute hari-lari-weekly-dryrun"),
        first("run jobs update hari-lari-weekly --region"),
        first("scheduler jobs create http hari-lari-report-weekly"),
    ]
    assert steps == sorted(steps)
    assert len(release.live_updates()) == 1


def test_a_failed_dry_run_leaves_the_live_job_and_the_schedules_alone(release):
    done = release.run("all", "bbb222", FAKE_EXECUTE_EXIT="1")
    assert done.returncode != 0
    assert release.live_updates() == []
    assert not any(c.startswith("scheduler ") for c in release.calls())


# --- the scheduler jobs (9.7) ---------------------------------------------------------------------

RUN_URI = (
    "https://run.googleapis.com/v2/projects/hari-lari/locations/asia-southeast2/"
    "jobs/hari-lari-weekly:run"
)
BODY = '{"overrides":{"containerOverrides":[{"args":["run","--report","weekly"]}]}}'


def scheduler_calls(release: Release) -> list[str]:
    return [c for c in release.calls() if c.startswith("scheduler ")]


def test_a_new_scheduled_report_gets_a_scheduler_job_that_runs_it(release):
    done = release.run("schedules", "bbb222")
    assert done.returncode == 0, done.stderr
    assert f"docker run --rm {IMAGES}:bbb222 schedules --json" in release.log.read_text()
    assert scheduler_calls(release) == [
        "scheduler jobs list --location asia-southeast2 --format value(name)",
        "scheduler jobs create http hari-lari-report-weekly --location asia-southeast2 "
        f"--schedule 0 4 * * 1 --time-zone Asia/Makassar --uri {RUN_URI} --http-method POST "
        "--oauth-service-account-email hari-lari-scheduler@hari-lari.iam.gserviceaccount.com "
        f"--message-body {BODY} --headers Content-Type=application/json",
        "scheduler jobs describe hari-lari-report-weekly --location asia-southeast2 "
        "--format value(state)",
    ]
    assert "scheduler job hari-lari-report-weekly: 0 4 * * 1 Asia/Makassar" in done.stdout


def test_an_existing_scheduler_job_is_updated_and_a_paused_one_resumed(release):
    names = "projects/hari-lari/locations/asia-southeast2/jobs/hari-lari-report-weekly\n"
    (release.fake / "schedulers").write_text(names)
    (release.fake / "state-hari-lari-report-weekly").write_text("PAUSED\n")
    assert release.run("schedules", "bbb222").returncode == 0
    calls = scheduler_calls(release)
    assert calls[1].startswith("scheduler jobs update http hari-lari-report-weekly ")
    assert calls[1].endswith("--update-headers Content-Type=application/json")
    assert calls[-1] == "scheduler jobs resume hari-lari-report-weekly --location asia-southeast2"


def test_a_disabled_schedule_is_paused(release):
    (release.fake / "schedules.json").write_text(json.dumps([{**WEEKLY_SCHEDULE[0],
                                                              "enabled": False}]))  # fmt: skip
    done = release.run("schedules", "bbb222")
    assert done.returncode == 0
    assert scheduler_calls(release)[-1] == (
        "scheduler jobs pause hari-lari-report-weekly --location asia-southeast2"
    )
    assert "(paused)" in done.stdout


def test_a_report_without_a_schedule_loses_its_job_and_other_jobs_stay(release):
    (release.fake / "schedulers").write_text(
        "projects/p/locations/r/jobs/hari-lari-report-weekly\n"
        "projects/p/locations/r/jobs/hari-lari-report-gone\n"
        "projects/p/locations/r/jobs/hari-lari-weekly\n"
    )
    assert release.run("schedules", "bbb222").returncode == 0
    deletes = [c for c in scheduler_calls(release) if " delete " in c]
    assert deletes == ["scheduler jobs delete hari-lari-report-gone --location asia-southeast2"]


def test_no_schedules_means_no_scheduler_job(release):
    (release.fake / "schedules.json").write_text("[]")
    assert release.run("schedules", "bbb222").returncode == 0
    assert scheduler_calls(release) == [
        "scheduler jobs list --location asia-southeast2 --format value(name)"
    ]


def test_a_failed_build_leaves_both_jobs_alone(release):
    done = release.run("all", "bbb222", FAKE_DOCKER_FAILS="build")
    assert done.returncode != 0
    assert not any(c.startswith("run jobs update") for c in release.calls())


def test_rollback_without_a_tag_goes_to_the_previous_release(release):
    release.job(
        "hari-lari-weekly",
        knative(f"{IMAGES}:bbb222", release="bbb222", **{"previous-release": "aaa111"}),
    )
    done = release.run("rollback")
    assert done.returncode == 0, done.stderr
    assert release.live_updates() == [
        f"run jobs update hari-lari-weekly --region asia-southeast2 --image {IMAGES}:aaa111 "
        "--update-labels release=aaa111,previous-release=bbb222"
    ]


def test_rollback_to_a_named_tag(release):
    done = release.run("rollback", "ccc333")
    assert done.returncode == 0, done.stderr
    assert f"--image {IMAGES}:ccc333" in release.live_updates()[0]


def test_rollback_without_a_recorded_release_asks_for_a_tag(release):
    done = release.run("rollback")
    assert done.returncode != 0
    assert "name the tag" in done.stderr
    assert release.live_updates() == []


def test_status_reads_both_shapes_of_the_describe_output(release):
    release.job(
        "hari-lari-weekly",
        knative(f"{IMAGES}:bbb222", release="bbb222", **{"previous-release": "aaa111"}),
    )
    release.job("hari-lari-weekly-dryrun", v2(f"{IMAGES}:bbb222"))
    done = release.run("status")
    assert done.returncode == 0, done.stderr
    lines = {line.split()[0]: line.split()[-1] for line in done.stdout.splitlines()}
    assert lines["hari-lari-weekly"] == f"{IMAGES}:bbb222"
    assert lines["hari-lari-weekly-dryrun"] == f"{IMAGES}:bbb222"
    assert lines["previous"] == "aaa111"


def test_an_unknown_command_prints_the_usage(release):
    done = release.run("deploy-everything")
    assert done.returncode == 1
    assert "deploy/release.sh promote [tag]" in done.stdout
