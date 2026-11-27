"""The landing zone and its manifest, the contract between a source and the warehouse (D26,
docs/architecture.md, "The landing manifest").

A source writes one run per date to `landing/<source>/<run date>/`: `tables/<table>.parquet`,
`raw/*.json.gz` (deleted by the bucket after 90 days), optional logs, and `manifest.json` last.
The manifest is the commit: a folder without it is incomplete and nothing reads it, and a
re-run deletes the old manifest before it writes its tables (Cloud Storage has no atomic
rename). The model lives here, in `core`, so the warehouse never imports a source; its JSON
schema is generated into git (`schema/landing_manifest.schema.json`).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from hari_lari_analytics.core.store import Store

MANIFEST = "manifest.json"
MANIFEST_VERSION = 1
LANDING = "landing"
RUN_ID = r"^\d{6}-[0-9a-z]{4}$"
IMAGE_ENV = "RELEASE"  # the image tag, baked in by deploy/release.sh


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TableEntry(_Strict):
    """One landed table. `events` are the records of the window; `snapshot` is the state at
    `finished_at` (the catalogue, the stock levels)."""

    kind: Literal["events", "snapshot"]
    file: str
    rows: int = Field(ge=0)
    columns: dict[str, str]  # the Arrow type of each column as written
    latest_at: str | None = None  # events: the newest record, local clock time


class Window(_Strict):
    since: str | None = None
    until: str | None = None


class Checks(_Strict):
    errors: list[str] = []
    warnings: list[str] = []


class Manifest(_Strict):
    manifest_version: Literal[1] = MANIFEST_VERSION
    source: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    run_date: date
    run_id: str = Field(pattern=RUN_ID)
    started_at: datetime
    finished_at: datetime
    image: str | None = None
    window: Window = Window()
    tables: dict[str, TableEntry]
    checks: Checks = Checks()
    source_info: dict[str, Any] = {}  # what only this source knows (the Moka token's expiry)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"


def run_key(source: str, run_date: date | str) -> str:
    """The folder of a run in a landing store (a store whose root is `landing/`)."""
    return f"{source}/{run_date}"


def new_run_id(now: datetime) -> str:
    """The local start time and four random hex digits: `040012-a3f9`."""
    return f"{now:%H%M%S}-{secrets.token_hex(2)}"


def image_tag() -> str | None:
    """The tag of the image this process runs in, or None outside one."""
    return os.environ.get(IMAGE_ENV) or None


def parquet_columns(path: Path) -> dict[str, str]:
    """The Arrow type of each column of a Parquet file, as written."""
    schema = pq.read_schema(path)
    return {name: str(schema.field(name).type) for name in schema.names}


def latest_at(df: pd.DataFrame, column: str = "created_at") -> str | None:
    if column not in df or df[column].isna().all():
        return None
    value = pd.to_datetime(df[column]).max()
    return value.isoformat()


def table_entry(
    staging: Path, name: str, df: pd.DataFrame, kind: Literal["events", "snapshot"]
) -> TableEntry:
    """The entry of a table already written to `staging/tables/<name>.parquet`."""
    file = f"tables/{name}.parquet"
    return TableEntry(
        kind=kind,
        file=file,
        rows=len(df),
        columns=parquet_columns(staging / file),
        latest_at=latest_at(df) if kind == "events" else None,
    )


def land(store: Store, manifest: Manifest, staging: Path) -> str:
    """Write a staged run (every file under `staging`, no manifest) to `<source>/<run date>/`
    of the landing store, then its manifest. Returns the run's URL."""
    key = run_key(manifest.source, manifest.run_date)
    store.delete(f"{key}/{MANIFEST}")
    for path in sorted(p for p in staging.rglob("*") if p.is_file() and p.name != MANIFEST):
        store.upload(f"{key}/{path.relative_to(staging).as_posix()}", path)
    store.write_text(f"{key}/{MANIFEST}", manifest.to_json())
    return store.url(key)


def landed_dates(store: Store, source: str) -> list[date]:
    """The run dates of a source that have a manifest, oldest first."""
    found = set()
    pattern = re.compile(rf"^{source}/(\d{{4}}-\d{{2}}-\d{{2}})/{MANIFEST}$")
    for key in store.list(source):
        match = pattern.match(key)
        if match:
            found.add(date.fromisoformat(match[1]))
    return sorted(found)


def read_manifest(store: Store, source: str, run_date: date) -> Manifest:
    text = store.read_text(f"{run_key(source, run_date)}/{MANIFEST}")
    return Manifest.model_validate_json(text)


def latest_landed(store: Store, source: str, on_or_before: date | None = None) -> date | None:
    """The newest landed run date of a source, at most `on_or_before`."""
    dates = [d for d in landed_dates(store, source) if on_or_before is None or d <= on_or_before]
    return dates[-1] if dates else None


# --- the JSON schema ------------------------------------------------------------------------------


def schema_path() -> Path:
    return Path(__file__).with_name("schema") / "landing_manifest.schema.json"


def schema_text() -> str:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **Manifest.model_json_schema(),
        "title": "Landing manifest",
        "description": "landing/<source>/<run date>/manifest.json (docs/architecture.md)",
    }
    return json.dumps(schema, indent=2) + "\n"


def write_schema() -> Path:
    path = schema_path()
    path.parent.mkdir(exist_ok=True)
    path.write_text(schema_text())
    return path
