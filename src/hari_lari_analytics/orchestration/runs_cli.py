"""hari-lari-runs: the run history from the ledger (docs/architecture.md, "The run ledger").

    hari-lari-runs list [--report weekly] [--since YYYY-MM-DD] [--dry-run]

prints one line per step record, oldest first: run date, run id, step, status, duration, the
anchor of a report and its deliveries, then the reason or the error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from hari_lari_analytics.core.store import DATA_ROOT_HELP, data_root, make_store
from hari_lari_analytics.orchestration.ledger import Record, read_records


def describe(record: Record) -> str:
    parts = [
        f"{record.run_date}",
        record.run_id,
        f"{record.step:<22}",
        f"{record.status:<7}",
        f"{record.seconds:>6.1f}s",
    ]
    if "anchor" in record.inputs:
        parts.append(f"anchor {record.inputs['anchor']}")
    if record.trigger == "scheduler":
        parts.append("(scheduled)")
    if record.to:
        parts.append(f"--to {record.to}")
    if record.force:
        parts.append("--force")
    deliveries = [
        f"{d.channel}{' ' + d.target if d.target else ''} {d.status}" for d in record.deliveries
    ]
    if deliveries:
        parts.append("→ " + ", ".join(deliveries))
    if record.reason:
        parts.append(f"reason: {record.reason}")
    if record.error and record.status in ("failed", "skipped"):
        parts.append(f"error: {record.error}")
    if record.warnings:
        parts.append(f"{len(record.warnings)} warning(s)")
    return "  ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hari-lari-runs", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="print the ledger records")
    listing.add_argument("--report", help="only the records of this report")
    listing.add_argument("--since", type=date.fromisoformat, help="the first run date")
    listing.add_argument("--dry-run", action="store_true", help="the ledger of the dry runs")
    listing.add_argument("--data-root", help=DATA_ROOT_HELP)
    return p


def main(argv: list[str] | None = None, out=None) -> int:
    from hari_lari_analytics.sources.moka.cli import load_env

    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    load_env()
    store = make_store(data_root(args.data_root))
    if args.dry_run:
        store = store.child("dry-run")
    step = f"report-{args.report}" if args.report else None
    records = read_records(store, since=args.since, step=step)
    for record in records:
        print(describe(record), file=out)
    if not records:
        print(f"No ledger records in {store.url('runs')}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
