"""The report CLI `hari-lari-report`: build, validate and list the schedules of the reports.

```text
hari-lari-report build --report weekly [--run data/extract] [--anchor YYYY-MM-DD]
                       [--out DIR] [--strict]
hari-lari-report gallery [--run data/extract] [--anchor YYYY-MM-DD] [--out DIR] [--no-png]
hari-lari-report validate
hari-lari-report schedules [--json]
hari-lari-report schema [--check]
```

`build` opens the latest extraction run, builds the models and the data checks, runs the report
and writes its outputs (PDF, PNG pages, Telegram text, workbook, JSON bundle) to
`data/output/reports/<report>/<anchor>/`; it never sends anything. `--strict` fails when a text
runs over its slot. `gallery` draws every component with a visual on the run, one page each, to
`data/output/gallery/` (plan 8.7). `validate` loads every component and report as strictly as a
build would and checks the three JSON Schemas; `schedules` lists the reports with a schedule, as
`deploy/` reads them. `--reports` and `--components` before the command point at other folders.
The commands `run` and `--scheduled` of docs/components.md ("Running a report") arrive with the
cut-over (plan 9.7).

The old local command that writes today's PDF and Excel is `hari-lari-report-legacy`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from hari_lari_analytics.load import InputError
from hari_lari_analytics.project import project
from hari_lari_analytics.reporting import components as components_module
from hari_lari_analytics.reporting import gallery
from hari_lari_analytics.reporting import reports as reports_module
from hari_lari_analytics.reporting.builder import build_report
from hari_lari_analytics.reporting.components import Component, ComponentError, components_dir
from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.outputs import bundle_schema
from hari_lari_analytics.reporting.queries import QueryContext
from hari_lari_analytics.reporting.reports import (
    SCHEMA_REF,
    Report,
    ReportError,
    expand,
    load_report,
    reports_dir,
)
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME

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
    build.add_argument("--config", type=Path, default=Path("config"), help="the config CSV folder")
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
    gallery.add_argument(
        "--config", type=Path, default=Path("config"), help="the config CSV folder"
    )
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
    db = open_run(args.run, args.config, anchor)
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
    db = open_run(args.run, args.config, args.anchor)
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
    """The JSON Schema files of components, reports and the bundle, with the text they should
    have: the report schema lists the packaged components."""
    packaged = sorted(p.stem for p in components_dir().glob("*.yml"))
    return [
        ("component", components_module.schema_path(), components_module.schema_text()),
        ("report", reports_module.schema_path(), reports_module.schema_text(packaged)),
        ("bundle", bundle_schema.schema_path(), bundle_schema.schema_text()),
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
    components_module.write_schema()
    print(f"Wrote {components_module.schema_path()}", file=out)
    packaged = sorted(p.stem for p in components_dir().glob("*.yml"))
    print(f"Wrote {reports_module.write_schema(packaged)}", file=out)
    print(f"Wrote {bundle_schema.write_schema()}", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out, err = sys.stdout, sys.stderr
    try:
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
