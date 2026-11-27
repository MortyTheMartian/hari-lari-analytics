"""`hari-lari-pipeline`: the daily tick, and the entrypoint of the job's image.

    hari-lari-pipeline tick [--date YYYY-MM-DD] [--only <report>] [--dry-run] [--to <target>]
                            [--force] [--all-scheduled] [--scheduler]
    hari-lari-pipeline schedule [--json]    the scheduler job of the tick, as deploy/ reads it

Any other command goes to `hari-lari-report` (`run`, `validate`, `schedules`, ...): a Cloud Run
job can override only the arguments of its image, not the command, so the image answers the
arguments of the jobs and the smoke test that predate the tick.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from hari_lari_analytics.core.config import config_location
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import data_root, make_store
from hari_lari_analytics.orchestration import report_cli
from hari_lari_analytics.orchestration.delivery import RunSettings
from hari_lari_analytics.orchestration.tick import TickOptions, run_tick
from hari_lari_analytics.presentation import reports as reports_module
from hari_lari_analytics.presentation.reports import reports_dir
from hari_lari_analytics.warehouse.inputs import InputError

TICK_JOB = "hari-lari-tick"  # the one Cloud Scheduler job
COMMANDS = ("tick", "schedule")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hari-lari-pipeline",
        description="The daily tick. Any other command goes to hari-lari-report.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser(
        "tick", help="extract, build the warehouse and run every report that is due"
    )
    tick.add_argument(
        "--date", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="the due date: which reports are due, and their anchor (default: today)",
    )  # fmt: skip
    tick.add_argument("--only", metavar="REPORT", help="only this report, if it is due")
    tick.add_argument(
        "--all-scheduled", action="store_true",
        help="every report with a schedule counts as due, enabled or not (the release check)",
    )  # fmt: skip
    tick.add_argument(
        "--scheduler", action="store_true",
        help="started by Cloud Scheduler: recorded as such, and the last tick of the day tells "
        "the chats of a report that is not sent that it is late",
    )  # fmt: skip
    tick.add_argument("--reports", type=Path, help="the folder of the report files")
    tick.add_argument("--components", type=Path, help="the folder of the component files")
    report_cli.add_job_options(tick)
    schedule = commands.add_parser("schedule", help="the Cloud Scheduler job of the tick")
    schedule.add_argument("--json", action="store_true", help="as JSON, for deploy/")
    return parser.parse_args(argv)


def schedule() -> dict:
    """The scheduler job: its cron from the tick times of project.yml, and the job's arguments."""
    p = project()
    return {
        "job": TICK_JOB,
        "cron": p.tick.cron,
        "timezone": p.timezone,
        "args": ["tick", "--scheduler"],
    }


def cmd_tick(args: argparse.Namespace) -> int:
    from hari_lari_analytics.core.log import JsonLog
    from hari_lari_analytics.sources.moka.cli import load_env

    load_env()
    log = JsonLog()
    settings = RunSettings(
        store=make_store(data_root(args.data_root)),
        config=config_location(args.config),
        dry_run=args.dry_run,
        force=args.force,
        to=args.to,
        drive_folder_id=args.drive_folder_id,
        trigger="scheduler" if args.scheduler else "manual",
    )
    components = report_cli.load_components(args.components)
    reports = reports_module.load_reports(components, args.reports or reports_dir())
    options = TickOptions(day=args.date, only=args.only, all_scheduled=args.all_scheduled)
    services = report_cli.run_services(args, log)
    return run_tick(list(reports.values()), settings, services, log, options)


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in (*COMMANDS, "-h", "--help"):
        return report_cli.main(argv)
    args = parse_args(argv)
    out = out or sys.stdout
    try:
        if args.command == "schedule":
            row = schedule()
            if args.json:
                print(json.dumps(row), file=out)
            else:
                print(f"{row['job']}: {row['cron']} {row['timezone']}", file=out)
            return 0
        return cmd_tick(args)
    except (ValueError, InputError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
