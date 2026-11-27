"""The report CLI `hari-lari-report`: run, build, validate and list the schedules of the reports.

```text
hari-lari-report run --report weekly [--anchor YYYY-MM-DD] [--dry-run] [--to admin] [--force]
                     [--run <run>]
hari-lari-report run --scheduled [--dry-run]
hari-lari-report build --report weekly [--run data/extract] [--anchor YYYY-MM-DD]
                       [--out DIR] [--strict]
hari-lari-report gallery [--run data/extract] [--anchor YYYY-MM-DD] [--out DIR] [--no-png]
hari-lari-report validate
hari-lari-report schedules [--json]
hari-lari-report schema [--check]
```

`run` is the job (plan 9.7, `delivery.py`): extract, model and check, build, store, deliver and
mark as sent; `--scheduled` runs every report with a schedule on one extraction. `build` opens
the latest extraction run, builds the models and the data checks, runs the report and writes its
outputs (PDF, PNG pages, Telegram text, workbook, JSON bundle) to
`data/output/reports/<report>/<anchor>/`; it never sends anything. `--strict` fails when a text
runs over its slot. `gallery` draws every component with a visual on the run, one page each, to
`data/output/gallery/` (plan 8.7). `validate` loads every component and report as strictly as a
build would and checks the three JSON Schemas; `schedules` lists the reports with a schedule, as
`deploy/` reads them. `--reports` and `--components` before the command point at other folders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.core import landing
from hari_lari_analytics.core.config import CONFIG_HELP, config_location
from hari_lari_analytics.core.project import project
from hari_lari_analytics.orchestration import delivery
from hari_lari_analytics.presentation import components as components_module
from hari_lari_analytics.presentation import gallery
from hari_lari_analytics.presentation import reports as reports_module
from hari_lari_analytics.presentation.builder import build_report
from hari_lari_analytics.presentation.components import Component, ComponentError, components_dir
from hari_lari_analytics.presentation.outputs import bundle_schema
from hari_lari_analytics.presentation.reports import (
    SCHEMA_REF,
    Report,
    ReportError,
    expand,
    load_report,
    reports_dir,
)
from hari_lari_analytics.presentation.visuals.theme import DEFAULT_THEME
from hari_lari_analytics.sources.moka.run import DEFAULT_SINCE
from hari_lari_analytics.warehouse.db import open_run
from hari_lari_analytics.warehouse.inputs import InputError

DEFAULT_OUT = Path("data/output/reports")
DEFAULT_GALLERY = Path("data/output/gallery")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hari-lari-report",
        description="Build, validate and schedule the reports of the reporting framework.",
    )
    parser.add_argument("--reports", type=Path, help="the folder of the report files")
    parser.add_argument("--components", type=Path, help="the folder of the component files")
    commands = parser.add_subparsers(dest="command", required=True)

    add_run_parser(commands)

    build = commands.add_parser("build", help="build the outputs of one report")
    build.add_argument("--report", required=True, help="a report id, or the path of its file")
    build.add_argument(
        "--run",
        default="data/extract",
        help="a run folder, or a folder (or gs://bucket/prefix) of dated runs; latest is used",
    )
    build.add_argument(
        "--anchor",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="last day of the report (default: the report's `anchor` parameter, else the rule "
        "of project.yml)",
    )
    build.add_argument("--config", help=CONFIG_HELP)
    build.add_argument(
        "--out", type=Path, help=f"the output folder (default: {DEFAULT_OUT}/<report>/<anchor>)"
    )
    build.add_argument("--strict", action="store_true", help="fail when a text runs over its slot")

    gallery = commands.add_parser(
        "gallery", help="draw every component on one run, one page each (a visual check)"
    )
    gallery.add_argument(
        "--run",
        default="data/extract",
        help="a run folder, or a folder (or gs://bucket/prefix) of dated runs; latest is used",
    )
    gallery.add_argument(
        "--anchor", type=date.fromisoformat, metavar="YYYY-MM-DD", help="last day of the data"
    )
    gallery.add_argument("--config", help=CONFIG_HELP)
    gallery.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_GALLERY,
        help=f"the output folder (default: {DEFAULT_GALLERY})",
    )
    gallery.add_argument("--no-png", action="store_true", help="write the PDF only")
    gallery.add_argument(
        "--strict", action="store_true", help="fail when a text runs over its slot"
    )

    commands.add_parser("validate", help="check every report, component and JSON Schema")

    schedules = commands.add_parser("schedules", help="list the reports that have a schedule")
    schedules.add_argument("--json", action="store_true", help="print JSON, for deploy/")

    schema = commands.add_parser(
        "schema", help="write the JSON Schemas of components, reports and the bundle"
    )
    schema.add_argument(
        "--check", action="store_true", help="only check that the files are up to date"
    )
    return parser.parse_args(argv)


def add_run_parser(commands) -> None:
    env = os.environ.get
    run = commands.add_parser(
        "run", help="extract, build, store and deliver a report (the job of every schedule)"
    )
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--report", help="a report id, or the path of its file")
    which.add_argument(
        "--scheduled", action="store_true", help="every report with a schedule, on one extraction"
    )
    run.add_argument(
        "--anchor", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="last day of the report (default: the report's `anchor` parameter, else the rule "
        "of project.yml)",
    )  # fmt: skip
    run.add_argument(
        "--dry-run", action="store_true",
        help="do everything except sending; files go under dry-run/",
    )  # fmt: skip
    run.add_argument(
        "--to", choices=sorted(project().telegram_targets),
        help="send every Telegram delivery to this chat instead; no sent-marker",
    )  # fmt: skip
    run.add_argument("--force", action="store_true", help="send even if it was already sent")
    run.add_argument(
        "--run", dest="existing_run",
        help="an existing run (a folder or gs://...) instead of a new extraction",
    )  # fmt: skip
    run.add_argument(
        "--data-root", default=env("DATA_ROOT", "data/jobs"),
        help="folder or gs://bucket[/prefix] for raw/, reports/, sent/",
    )  # fmt: skip
    run.add_argument("--config", help=CONFIG_HELP)
    run.add_argument(
        "--drive-folder-id", default=env("DRIVE_FOLDER_ID"),
        help="the Drive folder the report folders go into",
    )  # fmt: skip
    # The extraction: the same options and environment variables as hari-lari-extract
    run.add_argument("--since", default=DEFAULT_SINCE, help="first day of the extraction")
    run.add_argument(
        "--token-store", choices=["file", "secret"], default=env("TOKEN_STORE", "file")
    )
    run.add_argument(
        "--token-file", type=Path,
        default=Path(env("MOKA_TOKEN_FILE", "extraction/moka/.token.json")),
    )  # fmt: skip
    run.add_argument("--gcp-project", default=env("GCP_PROJECT"))
    run.add_argument("--token-secret", default=env("MOKA_TOKEN_SECRET", "moka-oauth-token"))


# --- run ------------------------------------------------------------------------------------------


def run_services(args: argparse.Namespace, log) -> delivery.Services:
    """The Moka extraction, Telegram, the chat ids and Drive, from secrets and options."""
    from hari_lari_analytics.distribution import settings as secrets
    from hari_lari_analytics.distribution.drive import DriveUploader, drive_session
    from hari_lari_analytics.distribution.telegram import TelegramClient
    from hari_lari_analytics.sources.moka.cli import make_store
    from hari_lari_analytics.sources.moka.client import MokaClient
    from hari_lari_analytics.sources.moka.run import run_extraction
    from hari_lari_analytics.sources.moka.tokens import TokenManager

    telegram: TelegramClient | None = None

    def get_telegram() -> TelegramClient:
        nonlocal telegram
        if telegram is None:
            telegram = TelegramClient(secrets.get_secret(secrets.BOT_TOKEN, args.gcp_project))
        return telegram

    def chat_id(target: str) -> str:
        settings = project()
        spec = (settings.telegram_env(target), settings.telegram_targets[target])
        return secrets.get_secret(spec, args.gcp_project)

    def extract(out: str):
        try:
            store = make_store(args)
        except SystemExit as error:  # a missing option; report it as a failed step
            raise RuntimeError(str(error)) from None
        client = MokaClient(TokenManager(store))
        return run_extraction(
            client, out, since=args.since, log=lambda m: log.info(m, step="extract")
        )

    def drive(folder_id: str) -> DriveUploader:
        get = lambda spec: secrets.get_secret(spec, args.gcp_project)  # noqa: E731
        session = drive_session(
            get(secrets.DRIVE_CLIENT_ID),
            get(secrets.DRIVE_CLIENT_SECRET),
            get(secrets.DRIVE_REFRESH_TOKEN),
        )
        return DriveUploader(session, folder_id)

    return delivery.Services(extract=extract, telegram=get_telegram, chat_id=chat_id, drive=drive)


def cmd_run(args: argparse.Namespace) -> int:
    from hari_lari_analytics.core.log import JsonLog
    from hari_lari_analytics.core.store import make_store
    from hari_lari_analytics.sources.moka.cli import load_env

    load_env()
    log = JsonLog()
    settings = delivery.RunSettings(
        store=make_store(args.data_root),
        config=config_location(args.config),
        anchor=args.anchor,
        dry_run=args.dry_run,
        force=args.force,
        to=args.to,
        run=args.existing_run,
        drive_folder_id=args.drive_folder_id,
    )
    services = run_services(args, log)
    components = load_components(args.components)
    if args.scheduled:
        directory = args.reports or reports_dir()
        reports = reports_module.load_reports(components, directory)
        return delivery.run_scheduled(list(reports.values()), settings, services, log)
    report = find_report(args.report, components, args.reports)
    return delivery.run_report_job(report, settings, services, log)


# --- loading --------------------------------------------------------------------------------------


def load_components(directory: Path | None) -> dict[str, Component]:
    return components_module.load_components(directory)


def find_report(reference: str, components: dict[str, Component], directory: Path | None) -> Report:
    """A report by id from the reports folder, or from a path to its file."""
    path = Path(reference)
    if path.suffix == ".yml" and path.exists():
        return load_report(path, components)
    folder = directory or reports_dir()
    path = folder / f"{reference}.yml"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in folder.glob("*.yml"))) or "none"
        raise ReportError(f"no report {reference} in {folder} (known: {known})")
    return load_report(path, components)


# --- build ----------------------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    components = load_components(args.components)
    report = find_report(args.report, components, args.reports)
    anchor = reports_module.resolve_anchor(report, args.anchor)
    db = open_run(args.run, config_location(args.config), anchor)
    ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
    print(
        f"Run {db.run.name}, data through {ctx.calendar.anchor:%d %b %Y}, "
        f"{len(db.checks.warnings)} data-check warning(s)",
        file=err,
    )
    target = args.out or DEFAULT_OUT / report.id / f"{ctx.calendar.anchor:%Y-%m-%d}"
    result = build_report(report, ctx, target)
    for path in result.files:
        print(path, file=out)
    for found in result.overflows:
        print(f"Warning: {found.describe()}", file=err)
    if result.overflows and args.strict:
        print(f"Error: {len(result.overflows)} text(s) run over their slots", file=err)
        return 1
    return 0


# --- gallery --------------------------------------------------------------------------------------


def cmd_gallery(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    components = load_components(args.components)
    db = open_run(args.run, config_location(args.config), args.anchor)
    ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
    print(f"Run {db.run.name}, data through {ctx.calendar.anchor:%d %b %Y}", file=err)
    result, skipped = gallery.build_gallery(
        components, ctx, args.out, png=not args.no_png, theme=DEFAULT_THEME
    )
    for path in result.files:
        print(path, file=out)
    for item in skipped:
        print(f"Skipped {item.component}: {item.reason}", file=err)
    for found in result.overflows:
        print(f"Warning: {found.describe()}", file=err)
    if result.overflows and args.strict:
        print(f"Error: {len(result.overflows)} text(s) run over their slots", file=err)
        return 1
    return 0


# --- validate -------------------------------------------------------------------------------------


def unapplied_filters(report: Report) -> list[str]:
    """For each filter of the report, the slots it does not apply to (the page and slot
    numbers), as lines for `validate`."""
    lines = []
    pages = expand(report, project().reference_date)
    for kind in ("where", "focus"):
        for dimension in getattr(report.spec.filters, kind):
            missed = [
                f"{p.id}/{i}"
                for p in pages
                for i, s in enumerate(p.slots, start=1)
                if not any(
                    f.origin == "report" and (f.kind, f.dimension) == (kind, dimension)
                    for f in s.ref.filters
                )
            ]
            if missed:
                lines.append(
                    f"filters.{kind}.{dimension} does not apply to slot(s): {', '.join(missed)}"
                )
    return lines


def cmd_validate(args: argparse.Namespace, out: TextIO) -> int:
    problems = 0
    try:
        components = load_components(args.components)
    except ComponentError as error:
        print(f"FAIL {error}", file=out)
        print("Fix the components first (hari-lari-component validate)", file=out)
        return 1
    directory = args.reports or reports_dir()
    paths = sorted(directory.glob("*.yml")) if directory.is_dir() else []
    for path in paths:
        try:
            report = load_report(path, components)
        except ReportError as error:
            problems += 1
            print(f"FAIL {error}", file=out)
            continue
        notes = []
        if not path.read_text().startswith(SCHEMA_REF):
            notes.append(f"the first line should be `{SCHEMA_REF}`")
        notes += unapplied_filters(report)
        pages = expand(report, project().reference_date)
        summary = f"{len(pages)} page(s)" + (
            f", schedule {report.spec.schedule.cron}" if report.spec.schedule else ""
        )
        print(f"ok   {report.id} ({'; '.join([summary, *notes])})", file=out)
    for _, path, text in schema_checks():
        if not path.exists() or path.read_text() != text:
            problems += 1
            print(f"FAIL schema/{path.name} is out of date: run hari-lari-report schema", file=out)
    print(
        f"{len(paths)} report(s), {len(components)} component(s), {problems} problem(s)", file=out
    )
    return 1 if problems else 0


def schema_checks() -> list[tuple[str, Path, str]]:
    """The JSON Schema files of components, reports, the bundle and the landing manifest, with
    the text they should have: the report schema lists the packaged components."""
    packaged = sorted(p.stem for p in components_dir().glob("*.yml"))
    return [
        ("component", components_module.schema_path(), components_module.schema_text()),
        ("report", reports_module.schema_path(), reports_module.schema_text(packaged)),
        ("bundle", bundle_schema.schema_path(), bundle_schema.schema_text()),
        ("landing manifest", landing.schema_path(), landing.schema_text()),
    ]


# --- schedules ------------------------------------------------------------------------------------


def schedules(reports: dict[str, Report]) -> list[dict]:
    """The reports that have a schedule, as `deploy/` reads them: what to start and when."""
    found = []
    for report in sorted(reports.values(), key=lambda r: r.id):
        s = report.spec.schedule
        if s is None:
            continue
        found.append(
            {
                "report": report.id,
                "cron": s.cron,
                "timezone": s.timezone or project().timezone,
                "enabled": s.enabled,
                "args": ["run", "--report", report.id],
            }
        )
    return found


def cmd_schedules(args: argparse.Namespace, out: TextIO) -> int:
    components = load_components(args.components)
    directory = args.reports or reports_dir()
    reports = reports_module.load_reports(components, directory)
    rows = schedules(reports)
    if args.json:
        print(json.dumps(rows, indent=2), file=out)
    else:
        for row in rows:
            state = "" if row["enabled"] else " (paused)"
            print(f"{row['report']}: {row['cron']} {row['timezone']}{state}", file=out)
        if not rows:
            print("No report has a schedule", file=out)
    return 0


# --- schema ---------------------------------------------------------------------------------------


def cmd_schema(args: argparse.Namespace, out: TextIO) -> int:
    checks = schema_checks()
    if args.check:
        stale = [p for _, p, text in checks if not p.exists() or p.read_text() != text]
        for _, path, _ in checks:
            print(f"{path} is {'out of date' if path in stale else 'up to date'}", file=out)
        return 1 if stale else 0
    for _, path, text in checks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"Wrote {path}", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out, err = sys.stdout, sys.stderr
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "build":
            return cmd_build(args, out, err)
        if args.command == "gallery":
            return cmd_gallery(args, out, err)
        if args.command == "validate":
            return cmd_validate(args, out)
        if args.command == "schedules":
            return cmd_schedules(args, out)
        return cmd_schema(args, out)
    except (ValueError, InputError) as error:
        print(f"Error: {error}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
