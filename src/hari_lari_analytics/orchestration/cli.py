"""The developer CLI `hari-lari-component`: run, validate and scaffold components.

```text
hari-lari-component run <id> [--period L6W] [--param name=value] [--anchor YYYY-MM-DD]
                             [--warehouse <date|path>] [--sql] [--csv]
                             [--png [FILE] [--watch] [--span 12] [--height 3]]
hari-lari-component validate
hari-lari-component new <id> [--from <id>] [--param name=value]
hari-lari-component manifest [--out FILE] [--check]
hari-lari-component schema [--check]
```

`run` opens a stored warehouse read-only (default: `warehouse/latest.json` under the data root),
runs the component with its facets and transforms, and prints each facet's result; `--sql` adds
the SQL with its parameters, `--csv` prints CSV instead of a table. `--png` draws the component
into a PNG at its default slot size, as a report page would (plan 8.6), and `--watch` draws it
again on every save. `validate` loads every component and report as strictly as a build would
and checks the JSON Schemas; `new` copies a component (or a minimal one) and writes the stub of
its snapshot; `manifest` writes the catalogue of the semantic layer, `semantic_manifest.json`
(plan 8.10); `schema` writes the JSON Schemas of the component and report files.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, TextIO

import jinja2
import pandas as pd
import yaml

from hari_lari_analytics.analysis.queries import QueryContext
from hari_lari_analytics.analysis.result import Result
from hari_lari_analytics.core.store import DATA_ROOT_HELP, data_root, make_store
from hari_lari_analytics.orchestration import report_cli
from hari_lari_analytics.presentation import components, manifest, preview
from hari_lari_analytics.presentation import reports as reports_module
from hari_lari_analytics.presentation.components import (
    NAME,
    SCHEMA_REF,
    Component,
    ComponentError,
    components_dir,
    load_component,
)
from hari_lari_analytics.presentation.facets import Facet, run_facets
from hari_lari_analytics.presentation.reports import ReportError, load_report, reports_dir
from hari_lari_analytics.presentation.snapshots import snapshot_text
from hari_lari_analytics.warehouse.build import WAREHOUSE_HELP, read_warehouse
from hari_lari_analytics.warehouse.inputs import InputError

PREVIEW_DIR = Path("data/output/preview")

TEMPLATE = """{schema_ref}
id: {id}
title: {title}
params: {{ period: L6W }}
query:
  model: fct_orders
  metrics: [net_sales, orders, aov]
  window: $period
  compare: prior
visual: {{ type: kpi_card }}
"""
_ID_LINE = re.compile(r"^id:.*$", re.MULTILINE)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hari-lari-component",
        description="Run, validate and scaffold the reporting components.",
    )
    parser.add_argument(
        "--components",
        type=Path,
        help="the folder of the component files (default: the packaged components/)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one component on an extraction run")
    run.add_argument("id")
    run.add_argument("--period", help="bind $period, such as L14D, L6W, L3M or full")
    _param_argument(run)
    run.add_argument(
        "--anchor",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="last day of the data (default: the anchor the warehouse was built with)",
    )
    run.add_argument("--warehouse", help=WAREHOUSE_HELP)
    run.add_argument("--data-root", help=DATA_ROOT_HELP)
    run.add_argument("--sql", action="store_true", help="also print the SQL and its parameters")
    run.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    run.add_argument(
        "--png",
        nargs="?",
        const="",
        metavar="FILE",
        help=f"draw the component into a PNG (default: {PREVIEW_DIR}/<id>.png) instead of "
        "printing its result",
    )
    run.add_argument(
        "--watch", action="store_true", help="with --png: draw again whenever the file changes"
    )
    run.add_argument(
        "--span",
        type=int,
        default=preview.DEFAULT_SPAN,
        help="with --png: the columns of the slot, 1 to 12 (default 12)",
    )
    run.add_argument(
        "--height",
        type=int,
        default=preview.DEFAULT_HEIGHT,
        help="with --png: the height of the slot in sixths of the page body (default 3)",
    )

    commands.add_parser("validate", help="check every component and the JSON Schema")

    new = commands.add_parser("new", help="scaffold a component and its snapshot")
    new.add_argument("id")
    new.add_argument("--from", dest="source", metavar="ID", help="copy this component")
    _param_argument(new, "the snapshot's value of a parameter without a default")
    new.add_argument(
        "--snapshots",
        type=Path,
        default=Path("tests/snapshots"),
        help="the folder of the snapshot files",
    )

    manifest = commands.add_parser(
        "manifest", help="write semantic_manifest.json, the catalogue of the semantic layer"
    )
    manifest.add_argument(
        "--out",
        type=Path,
        help="the file to write or check (default: the packaged semantic_manifest.json)",
    )
    manifest.add_argument(
        "--check", action="store_true", help="only check that the file is up to date"
    )

    schema = commands.add_parser("schema", help="write the JSON Schema of the component files")
    schema.add_argument(
        "--check", action="store_true", help="only check that the file is up to date"
    )
    return parser.parse_args(argv)


def _param_argument(parser: argparse.ArgumentParser, help: str | None = None) -> None:
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=help or "bind a parameter; the value is YAML (5, Coffee, [Cash, BCA])",
    )


def parse_params(pairs: list[str]) -> dict[str, Any]:
    params = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not re.match(NAME, name):
            raise ComponentError(f"--param {pair}: write name=value, with a snake_case name")
        params[name] = yaml.safe_load(value) if value else ""
    return params


# --- run ------------------------------------------------------------------------------------------


def find_component(directory: Path, component_id: str) -> Component:
    path = directory / f"{component_id}.yml"
    if not path.exists():
        known = sorted(p.stem for p in directory.glob("*.yml"))
        close = difflib.get_close_matches(component_id, known, n=3)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        listed = ", ".join(known) or "none"
        raise ComponentError(f"no component {component_id} in {directory} ({listed}){hint}")
    return load_component(path)


def render_text(template: str | None, params: Mapping, result: Result, facet: Facet) -> str:
    """A title or hint as the builder will render it; an undefined name is an error."""
    return components.render_text(template, params, result.meta, facet)


def _number(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.4g}"


def _table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(no rows)"
    shown = df.copy()
    for column in shown.columns:
        if pd.api.types.is_float_dtype(shown[column]):
            shown[column] = shown[column].map(lambda v: "" if pd.isna(v) else _number(v))
    with pd.option_context("display.max_columns", None, "display.width", 0):
        return shown.to_string(index=False)


def _describe(result: Result) -> str:
    meta = result.meta
    parts = [f"shape {meta.shape}", f"{len(result.df)} row(s)"]
    if meta.window is not None:
        window = meta.window
        parts.insert(0, f"{window.key}: {window.current.label}")
        if window.compare != "none":
            parts.insert(1, f"vs {window.compare_label}" if window.compare_ok else "no comparison")
        if meta.trading_days is not None:
            parts.append(f"{meta.trading_days} trading days")
    if meta.empty:
        parts.append("no sales")
    return " · ".join(parts)


def _sql(result: Result) -> str:
    if not result.meta.sql:
        return "-- a Python source, no SQL"
    params = "\n".join(f"--   ${k} = {v!r}" for k, v in result.meta.params.items())
    return result.meta.sql.strip() + ("\n-- parameters:\n" + params if params else "")


def print_facets(
    component: Component,
    params: dict[str, Any],
    facets: list[Facet],
    *,
    sql: bool,
    csv: bool,
    out: TextIO,
    err: TextIO,
) -> None:
    groups: dict[int, list[Facet]] = {}  # the facets of one result (by metric, they share it)
    for facet in facets:
        groups.setdefault(id(facet.result), []).append(facet)
    if csv:
        frames = [
            g[0].result.df.assign(facet=g[0].key) if g[0].label and g[0].metric is None
            else g[0].result.df
            for g in groups.values()
        ]  # fmt: skip
        pd.concat(frames, ignore_index=True).to_csv(out, index=False)
    bound = {**component.defaults, **params}
    for group in groups.values():
        facet, result = group[0], group[0].result
        try:
            title = render_text(component.raw.get("title"), bound, result, facet)
            hint = render_text(component.raw.get("hint"), bound, result, facet)
        except jinja2.TemplateError as error:
            raise ComponentError(f"{component.source}: title or hint: {error}") from None
        if not csv:
            labels = ", ".join(f.label for f in group if f.label)
            print(f"== {title} · {labels}" if labels else f"== {title}", file=out)
            if hint:
                print(f"   {hint}", file=out)
            print(f"   {_describe(result)}", file=out)
            print(_table(result.df) + "\n", file=out)
        if sql:
            print(_sql(result) + "\n", file=err if csv else out)


def cmd_run(args: argparse.Namespace, directory: Path, out: TextIO, err: TextIO) -> int:
    params = parse_params(args.param)
    if args.period:
        params["period"] = args.period
    component = find_component(directory, args.id)
    ignored = sorted(set(params) - component.parameters)
    if ignored:
        names = ", ".join(f"${p}" for p in ignored)
        print(f"Note: {component.id} does not use {names}", file=err)
    component.bind(params)  # fail on a parameter before building the models
    if args.watch and args.png is None:
        raise ComponentError("--watch draws a PNG: add --png")
    with read_warehouse(make_store(data_root(args.data_root)), args.warehouse, args.anchor) as db:
        ctx = QueryContext.create(db.con, run=db.run, checks=db.checks)
        print(
            f"Run {db.run.name}, data through {ctx.calendar.anchor:%d %b %Y}, "
            f"{len(db.checks.warnings)} data-check warning(s)",
            file=err,
        )
        if args.png is not None:
            return cmd_preview(args, directory, ctx, params, out, err)
        facets = run_facets(component, ctx, params)
        if not facets:
            print("No sales in the component's window: nothing to show", file=err)
            return 0
        print_facets(component, params, facets, sql=args.sql, csv=args.csv, out=out, err=err)
    return 0


def cmd_preview(
    args: argparse.Namespace,
    directory: Path,
    ctx: QueryContext,
    params: dict[str, Any],
    out: TextIO,
    err: TextIO,
    watch: Callable[..., None] = preview.watch,
) -> int:
    """`run --png`: draw the component into its slot (again on every save with `--watch`)."""
    path = Path(args.png) if args.png else PREVIEW_DIR / f"{args.id}.png"

    def draw() -> None:
        component = find_component(directory, args.id)  # read again: the file may have changed
        sources = [find_component(directory, n) for n in component.raw.get("insights_from") or []]
        overflows = preview.render_preview(
            component, ctx, params, path, args.span, args.height, sources=sources
        )
        for found in overflows:
            print(f"Warning: {found.describe()}", file=err)
        print(f"Wrote {path}", file=out)

    if args.watch:
        print(f"Watching {directory / (args.id + '.yml')}, Ctrl-C to stop", file=err)
        watch(directory / f"{args.id}.yml", draw, err)
    else:
        draw()
    return 0


# --- validate -------------------------------------------------------------------------------------


def cmd_validate(directory: Path, out: TextIO) -> int:
    paths = sorted(directory.glob("*.yml"))
    problems = 0
    loaded: dict[str, Component] = {}
    for path in paths:
        try:
            component = load_component(path)
        except ComponentError as error:
            problems += 1
            print(f"FAIL {error}", file=out)
            continue
        loaded[component.id] = component
        notes = []
        if not path.read_text().startswith(SCHEMA_REF):
            notes.append(f"the first line should be `{SCHEMA_REF}`")
        if component.unbound:
            names = ", ".join(f"${p}" for p in sorted(component.unbound))
            notes.append(f"checked without {names}, which a report binds")
        print(f"ok   {component.id}" + (f" ({'; '.join(notes)})" if notes else ""), file=out)
    for _, path, text in report_cli.schema_checks():
        if not path.exists() or path.read_text() != text:
            problems += 1
            print(f"FAIL schema/{path.name} is out of date: run hari-lari-component schema",
                  file=out)  # fmt: skip
    if directory == components_dir() and len(loaded) == len(paths):
        path = manifest.manifest_path()
        text = manifest.manifest_text(loaded, load_reports_of(loaded))
        if not path.exists() or path.read_text() != text:
            problems += 1
            print(f"FAIL {path.name} is out of date: run hari-lari-component manifest", file=out)
    packaged = directory == components_dir() and reports_dir().is_dir()
    report_paths = sorted(reports_dir().glob("*.yml")) if packaged else []
    for path in report_paths:
        try:
            report = load_report(path, loaded)
        except ReportError as error:
            problems += 1
            print(f"FAIL {error}", file=out)
        else:
            print(f"ok   report {report.id}", file=out)
    print(
        f"{len(paths)} component(s), {len(report_paths)} report(s), {problems} problem(s)",
        file=out,
    )
    return 1 if problems else 0


# --- new ------------------------------------------------------------------------------------------


def scaffold(component_id: str, directory: Path, source: str | None) -> str:
    """The text of a new component: a copy of `source` with the new id, or the template."""
    if source is None:
        title = component_id.replace("_", " ").capitalize()
        return TEMPLATE.format(schema_ref=SCHEMA_REF, id=component_id, title=title)
    text = (directory / f"{find_component(directory, source).id}.yml").read_text()
    text, count = _ID_LINE.subn(f"id: {component_id}", text, count=1)
    if not count:
        raise ComponentError(f"components/{source}.yml: no `id:` line to replace")
    return text if text.startswith(SCHEMA_REF) else f"{SCHEMA_REF}\n{text}"


def cmd_new(args: argparse.Namespace, directory: Path, out: TextIO) -> int:
    if not re.match(NAME, args.id):
        raise ComponentError(f"{args.id}: a component id is snake_case")
    path = directory / f"{args.id}.yml"
    if path.exists():
        raise ComponentError(f"{path} exists already")
    text = scaffold(args.id, directory, args.source)
    params = parse_params(args.param)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    try:
        component = load_component(path)
        missing = sorted(component.unbound - set(params))
        if missing:
            names = ", ".join(f"{p}=..." for p in missing)
            raise ComponentError(f"the snapshot needs a value for each of: --param {names}")
        component.bind(params)
    except ComponentError:
        path.unlink()
        raise
    print(f"Wrote {path}", file=out)
    snapshot = args.snapshots / f"{args.id}.yml"
    if snapshot.exists():
        print(f"Kept {snapshot}", file=out)
    elif args.snapshots.is_dir():
        used = {k: v for k, v in params.items() if k in component.parameters}
        snapshot.write_text(snapshot_text(args.id, used))
        print(f"Wrote {snapshot}", file=out)
    else:
        print(f"No snapshot: {args.snapshots} is not a folder", file=out)
    print(
        "Next: edit the YAML, then\n"
        f"  uv run hari-lari-component run {args.id} --sql\n"
        "  uv run hari-lari-component validate\n"
        f"  uv run pytest tests/test_snapshots.py --update-snapshots -k {args.id}",
        file=out,
    )
    return 0


# --- schema ---------------------------------------------------------------------------------------


def cmd_schema(check: bool, out: TextIO) -> int:
    """Write (or with `check` compare) the JSON Schemas of the component and report files."""
    checks = report_cli.schema_checks()
    if check:
        stale = [p for _, p, text in checks if not p.exists() or p.read_text() != text]
        for _, path, _ in checks:
            print(f"{path} is {'out of date' if path in stale else 'up to date'}", file=out)
        return 1 if stale else 0
    for _, path, text in checks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"Wrote {path}", file=out)
    return 0


# --- manifest -------------------------------------------------------------------------------------


def cmd_manifest(args: argparse.Namespace, directory: Path, out: TextIO) -> int:
    """Write (or with `--check` compare) the semantic manifest of the components in
    `directory` (default: the packaged ones)."""
    loaded = components.load_components(directory)
    text = manifest.manifest_text(loaded, load_reports_of(loaded))
    path = args.out or manifest.manifest_path()
    if args.check:
        stale = not path.exists() or path.read_text() != text
        print(f"{path} is {'out of date' if stale else 'up to date'}", file=out)
        return 1 if stale else 0
    manifest.write_manifest(path, text)
    print(f"Wrote {path}", file=out)
    return 0


def load_reports_of(loaded: Mapping[str, Component]) -> dict:
    return reports_module.load_reports(loaded)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = args.components or components_dir()
    out, err = sys.stdout, sys.stderr
    try:
        if args.command == "run":
            return cmd_run(args, directory, out, err)
        if args.command == "validate":
            return cmd_validate(directory, out)
        if args.command == "new":
            return cmd_new(args, directory, out)
        if args.command == "manifest":
            return cmd_manifest(args, directory, out)
        return cmd_schema(args.check, out)
    except (ValueError, InputError) as error:
        print(f"Error: {error}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
