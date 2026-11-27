"""The stored warehouse (D27, docs/architecture.md, "The warehouse"): built once a run date
from the newest landing run of each source and the config, then read by every report.

`warehouse/<date>/` holds `warehouse.duckdb` (the raw_ and seed_ tables, every model, the
data-check results and the landing runs it was built from), `marts/<model>.parquet` (the
exposed models without their `private` columns, for other tools) and `build.json`, written
last. `warehouse/latest.json` names the newest build whose checks passed; a build whose error
checks fail writes nothing. The models are cut at the default anchor of the run date
(project.yml); a report with another anchor rebuilds them in memory from the stored raw_ and
seed_ tables (`open_warehouse`).
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict

from hari_lari_analytics.core.config import file_hashes, local_config
from hari_lari_analytics.core.landing import (
    LANDING,
    Manifest,
    image_tag,
    latest_landed,
    read_manifest,
    run_key,
)
from hari_lari_analytics.core.project import Project, project
from hari_lari_analytics.core.store import Store, make_store
from hari_lari_analytics.warehouse.checks import (
    Check,
    CheckReport,
    CheckResult,
    DataCheckError,
    check,
    run_checks,
)
from hari_lari_analytics.warehouse.db import (
    SEED_FILES,
    SEED_SOURCES,
    RunDb,
    Seeds,
    build_on_inputs,
    connect,
    source_names,
    source_tables,
)
from hari_lari_analytics.warehouse.inputs import ApiRun, InputError
from hari_lari_analytics.warehouse.model_specs import default_models

WAREHOUSE = "warehouse"
DB_FILE = "warehouse.duckdb"
BUILD_FILE = "build.json"
LATEST = f"{WAREHOUSE}/latest.json"
# the source whose run names a build (a bundle's `run`) and whose start is the snapshot time
MAIN_SOURCE = "moka"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LandedRun(_Strict):
    run_date: date
    run_id: str
    started_at: datetime
    location: str


class CheckCounts(_Strict):
    checks: int
    warnings: list[str] = []


class BuildInfo(_Strict):
    """`build.json` of a warehouse, and `latest.json` for the newest one."""

    date: date
    anchor: date
    built_at: datetime
    seconds: float
    image: str | None = None
    database: str  # the key of warehouse.duckdb under the data root
    landing: dict[str, LandedRun]
    config: dict[str, str]  # file name: the first 12 hex digits of its sha256
    checks: CheckCounts
    marts: dict[str, int]  # model: rows

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"


def warehouse_key(run_date: date | str) -> str:
    return f"{WAREHOUSE}/{run_date}"


def landed_runs(landing: Store, run_date: date) -> dict[str, Manifest]:
    """The manifest of the newest run of each source landed on or before `run_date`."""
    manifests = {}
    for source in source_names():
        landed = latest_landed(landing, source, run_date)
        if landed is None:
            raise InputError(f"No landing run of {source} on or before {run_date}")
        manifests[source] = read_manifest(landing, source, landed)
    return manifests


def _read_tables(landing: Store, manifest: Manifest, folder: Path) -> dict[str, pd.DataFrame]:
    tables = {}
    for name in source_tables(manifest.source):
        entry = manifest.tables.get(name)
        if entry is None:
            continue  # sources.yml decides whether that is an error
        path = folder / manifest.source / entry.file
        landing.download(f"{run_key(manifest.source, manifest.run_date)}/{entry.file}", path)
        tables[name] = pd.read_parquet(path)
    return tables


def build_warehouse(
    root: Store,
    run_date: date,
    config: str | Path,
    *,
    landing: Store | None = None,
    settings: Project | None = None,
    now: datetime | None = None,
) -> BuildInfo:
    """Build the warehouse of `run_date` into `root` (default input: `root`'s landing/).

    A source without a landing run, a missing config file or a failing error check raises,
    and nothing is written.
    """
    started = time.monotonic()
    settings = settings or project()
    landing = landing or root.child(LANDING)
    manifests = landed_runs(landing, run_date)
    anchor = settings.anchor_for(run_date)
    main = manifests[MAIN_SOURCE]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        tables: dict[str, pd.DataFrame] = {}
        for manifest in manifests.values():
            tables.update(_read_tables(landing, manifest, folder / "landing"))
        with local_config(config, [file for file, _ in SEED_FILES.values()]) as config_dir:
            seeds = Seeds.from_dir(config_dir)
            hashes = file_hashes(config_dir)
        db_path = folder / DB_FILE
        con = connect(
            tables, seeds, anchor, settings,
            extracted_at=main.started_at.replace(tzinfo=None), database=str(db_path),
        )  # fmt: skip
        del tables
        report = run_checks(con)
        landed = {
            source: LandedRun(
                run_date=m.run_date,
                run_id=m.run_id,
                started_at=m.started_at,
                location=landing.url(run_key(source, m.run_date)),
            )  # fmt: skip
            for source, m in manifests.items()
        }
        _store_context(con, manifests, landed, report)
        if report.errors:
            con.close()
            raise DataCheckError(report)
        marts = _write_marts(con, folder / "marts")
        con.close()
        key = warehouse_key(run_date)
        info = BuildInfo(
            date=run_date,
            anchor=anchor,
            built_at=now or settings.now(),
            seconds=round(time.monotonic() - started, 1),
            image=image_tag(),
            database=f"{key}/{DB_FILE}",
            landing=landed,
            config=hashes,
            checks=CheckCounts(
                checks=len(report.results), warnings=[r.describe() for r in report.warnings]
            ),
            marts=marts,
        )
        root.delete(f"{key}/{BUILD_FILE}")
        root.upload(f"{key}/{DB_FILE}", db_path)
        for model in marts:
            root.upload(f"{key}/marts/{model}.parquet", folder / "marts" / f"{model}.parquet")
        root.write_text(f"{key}/{BUILD_FILE}", info.to_json())
    latest = read_latest(root)
    if latest is None or latest.date <= run_date:
        root.write_text(LATEST, info.to_json())
    return info


def _store_context(
    con: duckdb.DuckDBPyConnection,
    manifests: dict[str, Manifest],
    landed: dict[str, LandedRun],
    report: CheckReport,
) -> None:
    con.execute(
        "create table warehouse_landing "
        "(source varchar, run_date date, run_id varchar, location varchar, manifest varchar)"
    )
    for source, m in manifests.items():
        con.execute(
            "insert into warehouse_landing values (?, ?, ?, ?, ?)",
            [source, m.run_date, m.run_id, landed[source].location, m.model_dump_json()],
        )
    con.execute(
        "create table warehouse_checks (name varchar, severity varchar, message varchar, "
        "failures bigint, sample varchar)"
    )
    for r in report.results:
        sample = json.dumps([[str(v) for v in row] for row in r.sample])
        con.execute(
            "insert into warehouse_checks values (?, ?, ?, ?, ?)",
            [r.check.name, r.check.severity, r.check.message, r.failures, sample],
        )


def _write_marts(con: duckdb.DuckDBPyConnection, folder: Path) -> dict[str, int]:
    folder.mkdir(parents=True, exist_ok=True)
    rows = {}
    for name, spec in default_models().items():
        if not spec.exposed:
            continue
        hidden = f" exclude ({', '.join(spec.private)})" if spec.private else ""
        path = folder / f"{name}.parquet"
        con.execute(f"copy (select *{hidden} from {name}) to '{path}' (format parquet)")
        rows[name] = con.execute(f"select count(*) from {name}").fetchone()[0]
    return rows


def read_latest(root: Store) -> BuildInfo | None:
    if not root.exists(LATEST):
        return None
    return BuildInfo.model_validate_json(root.read_text(LATEST))


def read_build(root: Store, run_date: date | str) -> BuildInfo:
    key = f"{warehouse_key(run_date)}/{BUILD_FILE}"
    if not root.exists(key):
        raise InputError(f"No warehouse of {run_date} in {root.url(WAREHOUSE)}")
    return BuildInfo.model_validate_json(root.read_text(key))


# --- reading a stored warehouse ----------------------------------------------------------------

WAREHOUSE_HELP = (
    "the warehouse to read: a run date (warehouse/<date>/ under the data root), or the path or "
    "gs:// URL of a warehouse.duckdb or of its folder (default: warehouse/latest.json)"
)


def warehouse_url(root: Store, ref: str | None = None) -> str:
    """The URL of the `warehouse.duckdb` that `ref` names (see `WAREHOUSE_HELP`)."""
    if ref is None:
        latest = read_latest(root)
        if latest is None:
            raise InputError(
                f"No warehouse in {root.url(WAREHOUSE)} (no latest.json): "
                "run hari-lari-warehouse build"
            )
        return root.url(latest.database)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref):
        return root.url(read_build(root, ref).database)
    return ref if ref.endswith(".duckdb") else f"{ref.rstrip('/')}/{DB_FILE}"


def local_warehouse(url: str, folder: Path) -> Path:
    """A local path of the file at `url`: itself, or a download into `folder` for gs://."""
    if url.startswith("gs://"):
        parent, _, name = url.rpartition("/")
        store = make_store(parent)
        if not store.exists(name):
            raise InputError(f"No warehouse file {url}")
        store.download(name, folder / name)
        return folder / name
    if not Path(url).is_file():
        raise InputError(f"No warehouse file {url}")
    return Path(url)


@contextmanager
def read_warehouse(
    root: Store, ref: str | None = None, anchor: date | None = None, settings: Project | None = None
) -> Iterator[RunDb]:
    """Open the warehouse `ref` names (`warehouse_url`) for the duration of the block."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_warehouse(local_warehouse(warehouse_url(root, ref), Path(tmp)), anchor, settings)
        try:
            yield db
        finally:
            db.con.close()


def open_warehouse(
    path: Path, anchor: date | None = None, settings: Project | None = None
) -> RunDb:
    """The models of a `warehouse.duckdb`, opened read-only, with its stored check results.

    With an `anchor` (or a first weekday) other than the stored one, the models are rebuilt in
    memory from the stored raw_ and seed_ tables and checked again.
    """
    settings = settings or project()
    con = duckdb.connect(str(path), read_only=True)
    stored_anchor, first_weekday, extracted_at = con.execute(
        "select anchor, first_weekday, extracted_at from run_context"
    ).fetchone()
    run = _stored_run(con)
    if anchor in (None, stored_anchor) and first_weekday == settings.first_isoweekday:
        return RunDb(run, con, _stored_checks(con))
    inputs = [
        name
        for (name,) in con.execute("select table_name from duckdb_tables()").fetchall()
        if name.startswith("raw_") or name in {f"seed_{n}" for n in SEED_SOURCES}
    ]
    con.close()
    memory = duckdb.connect()
    quoted = str(path).replace("'", "''")
    memory.execute(f"attach '{quoted}' as stored (read_only)")
    for name in inputs:
        memory.execute(f"create table {name} as select * from stored.{name}")
    memory.execute("detach stored")
    build_on_inputs(memory, anchor or stored_anchor, settings, extracted_at)
    return RunDb(run, memory, check(memory))


def _stored_run(con: duckdb.DuckDBPyConnection) -> ApiRun:
    row = con.execute(
        "select location, manifest from warehouse_landing where source = ?", [MAIN_SOURCE]
    ).fetchone()
    if row is None:
        raise InputError(f"The warehouse has no landing run of {MAIN_SOURCE}")
    location, manifest = row
    return ApiRun(location, {}, json.loads(manifest))


def _stored_checks(con: duckdb.DuckDBPyConnection) -> CheckReport:
    rows = con.execute(
        "select name, severity, message, failures, sample from warehouse_checks"
    ).fetchall()
    return CheckReport([
        CheckResult(
            Check(name, severity, "", message), failures, [tuple(r) for r in json.loads(sample)]
        )
        for name, severity, message, failures, sample in rows
    ])  # fmt: skip
