"""hari-lari-warehouse: build the stored warehouse (docs/architecture.md, "The warehouse").

    hari-lari-warehouse build [--date YYYY-MM-DD] [--dry-run]

reads the newest landing run of each source on or before the date (default: today) and the
config, builds the models, runs the data checks and writes `warehouse/<date>/` and
`warehouse/latest.json` under the data root. A failing error check writes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from hari_lari_analytics.core.config import CONFIG_HELP, ConfigError, config_location
from hari_lari_analytics.core.landing import LANDING
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import make_store
from hari_lari_analytics.warehouse.build import build_warehouse
from hari_lari_analytics.warehouse.inputs import InputError

DRY_RUN = "dry-run"


def data_root(value: str | None) -> str:
    """The data root: the value, else $DATA_ROOT, else `data`."""
    return value or os.environ.get("DATA_ROOT") or "data"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hari-lari-warehouse", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build the warehouse of a run date")
    b.add_argument("--date", type=date.fromisoformat,
                   help="the run date (default: today); the newest landing run of each source "
                        "on or before it is read")  # fmt: skip
    b.add_argument("--dry-run", action="store_true",
                   help="write under dry-run/ of the data root, never the live latest.json "
                        "(the landing runs are still read from the data root)")  # fmt: skip
    b.add_argument("--data-root", help="a folder or gs:// URL (default: $DATA_ROOT, else data)")
    b.add_argument("--config", help=CONFIG_HELP)
    return p


def main(argv: list[str] | None = None) -> int:
    from hari_lari_analytics.sources.moka.cli import load_env

    args = build_parser().parse_args(argv)
    load_env()
    settings = project()
    root = make_store(data_root(args.data_root))
    out = root.child(DRY_RUN) if args.dry_run else root
    run_date = args.date or settings.today()
    try:
        info = build_warehouse(
            out, run_date, config_location(args.config),
            landing=root.child(LANDING), settings=settings,
        )  # fmt: skip
    except (InputError, ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    landed = ", ".join(f"{s} {r.run_date} ({r.run_id})" for s, r in info.landing.items())
    print(f"warehouse {info.date} (anchor {info.anchor}) from {landed} in {info.seconds}s")
    print(f"  {out.url(info.database)}")
    print(f"  {info.checks.checks} checks, {len(info.checks.warnings)} warning(s)")
    for warning in info.checks.warnings:
        print(f"    - {warning}")
    print("  marts: " + ", ".join(f"{m} {n}" for m, n in info.marts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
