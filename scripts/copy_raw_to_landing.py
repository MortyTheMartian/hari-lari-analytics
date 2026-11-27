"""Copy the Moka extraction runs of before the landing zone into it (plan t12, one-off).

    uv run --extra gcp python scripts/copy_raw_to_landing.py --src gs://hari-lari-data/raw \
        --dest gs://hari-lari-data/landing
    uv run python scripts/copy_raw_to_landing.py --src data/extract --dest data/landing

Each `<src>/<date>/` with a manifest becomes `<dest>/moka/<date>/`: the same Parquet tables and
request log, the raw JSON gzipped, and the manifest converted to the landing shape
(`core/landing.py`), so the history of the catalogue and stock snapshots is kept. A date that
already landed is left alone unless `--force`. Prints every run it copied with its row counts.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from hari_lari_analytics.core.landing import (
    MANIFEST,
    Checks,
    Manifest,
    Window,
    land,
    landed_dates,
    latest_at,
    parquet_columns,
)
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import Store, make_store
from hari_lari_analytics.sources.moka.fetch import CATALOG_TABLES
from hari_lari_analytics.sources.moka.run import SOURCE

DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})/manifest\.json$")


def old_runs(src: Store) -> list[str]:
    return sorted(m[1] for key in src.list() if (m := DATED.match(key)))


def convert(old: dict, run_date: date, staging: Path, origin: str) -> Manifest:
    """The landing manifest of an old run whose files are in `staging`."""
    tz = project().tz
    started = datetime.fromisoformat(old["run_at"])
    since = datetime.fromisoformat(old.get("since") or "2026-01-01").replace(tzinfo=tz)
    until = datetime.fromisoformat(old["until"]).replace(tzinfo=tz) if old.get("until") else None
    tables = {}
    for path in sorted((staging / "tables").glob("*.parquet")):
        name = path.stem
        kind = "snapshot" if name in CATALOG_TABLES else "events"
        rows = pq.read_metadata(path).num_rows
        expected = old.get("row_counts", {}).get(name)
        if expected is not None and expected != rows:
            raise ValueError(f"{origin}: {name} has {rows} rows, its manifest says {expected}")
        newest = None
        if kind == "events" and "created_at" in parquet_columns(path):
            newest = latest_at(pd.read_parquet(path, columns=["created_at"]))
        tables[name] = {
            "kind": kind, "file": f"tables/{name}.parquet", "rows": rows,
            "columns": parquet_columns(path), "latest_at": newest,
        }  # fmt: skip
    return Manifest(
        source=SOURCE,
        run_date=run_date,
        run_id=f"{started:%H%M%S}-copy",
        started_at=started,
        finished_at=started + timedelta(seconds=old.get("duration_seconds") or 0),
        window=Window(
            since=since.isoformat(), until=(until or started).isoformat(timespec="seconds")
        ),
        tables=tables,
        checks=Checks(errors=old.get("errors", []), warnings=old.get("warnings", [])),
        source_info={
            "http_calls": old.get("http_calls"),
            "token_expires_at": old.get("token_expires_at"),
            "token_refreshed": old.get("token_refreshed"),
            "copied_from": origin,
        },
    )


def copy_run(src: Store, dest: Store, day: str) -> Manifest:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for key in src.list(day):
            rel = key[len(day) + 1 :]
            if rel == MANIFEST:
                continue
            if rel.startswith("raw/") and rel.endswith(".json"):
                src.download(key, staging / "plain.json")
                (staging / rel).parent.mkdir(parents=True, exist_ok=True)
                with (staging / "plain.json").open("rb") as f, gzip.open(
                    staging / f"{rel}.gz", "wb"
                ) as out:
                    shutil.copyfileobj(f, out)
                (staging / "plain.json").unlink()
                continue
            src.download(key, staging / rel)
        old = json.loads(src.read_text(f"{day}/{MANIFEST}"))
        manifest = convert(old, date.fromisoformat(day), staging, src.url(day))
        land(dest, manifest, staging)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--src", required=True, help="the folder of dated runs (raw/)")
    parser.add_argument("--dest", required=True, help="the landing folder (landing/)")
    parser.add_argument("--force", action="store_true", help="replace dates that already landed")
    args = parser.parse_args(argv)
    src, dest = make_store(args.src), make_store(args.dest)
    landed = {str(d) for d in landed_dates(dest, SOURCE)}
    days = old_runs(src)
    if not days:
        print(f"no runs with a manifest in {args.src}", file=sys.stderr)
        return 1
    for day in days:
        if day in landed and not args.force:
            print(f"{day}: already landed in {dest.url(f'{SOURCE}/{day}')}, skipped")
            continue
        manifest = copy_run(src, dest, day)
        counts = ", ".join(f"{name} {t.rows}" for name, t in manifest.tables.items())
        print(f"{day}: copied to {dest.url(f'{SOURCE}/{day}')} ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
