"""The inputs of the models: the Parquet tables of one extraction run, and the config CSV files
(the seeds), each read and validated before `db.py` registers it."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ALIAS_COLUMNS = ["sales_item", "adjusted_item", "adjusted_category"]

CATEGORY_GROUP_COLUMNS = ["category", "category_group_level_1", "category_group_level_2"]
CATEGORY_GROUPS_LEVEL_1 = ["food", "drink", "consignment"]

CALENDAR_EVENT_COLUMNS = ["date_from", "date_to", "kind", "name"]
CALENDAR_EVENT_KINDS = ["holiday", "closure", "promotion", "other"]


class InputError(Exception):
    """The input files are missing, ambiguous or do not have the expected structure."""


# ---- the extraction run -------------------------------------------------------------


@dataclass
class ApiRun:
    """The tables of one extraction run, with the manifest if there is one."""

    location: str
    tables: dict[str, pd.DataFrame]
    manifest: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.location.rstrip("/").rsplit("/", 1)[-1]


def _is_gcs(location: str) -> bool:
    return location.startswith("gs://")


def find_run(location: str | Path) -> str:
    """Return the folder of one run.

    `location` is either a run folder (it holds `tables/`) or a folder of dated runs, in which
    case the most recent run wins. `gs://bucket/prefix` works the same way.
    """
    location = str(location)
    if _is_gcs(location):
        return _find_gcs_run(location)
    root = Path(location)
    if (root / "tables").is_dir():
        return str(root)
    runs = sorted(p for p in root.glob("*") if (p / "tables").is_dir()) if root.is_dir() else []
    if not runs:
        raise InputError(f"No extraction run (a folder with tables/) found in {root}")
    return str(runs[-1])


def _gcs_client():
    from google.cloud import storage  # needs the `gcp` extra

    return storage.Client()


def _split_gcs(location: str) -> tuple[str, str]:
    bucket, _, prefix = location[len("gs://") :].partition("/")
    return bucket, prefix.strip("/")


def _find_gcs_run(location: str, client=None) -> str:
    client = client or _gcs_client()
    bucket, prefix = _split_gcs(location)
    base = f"{prefix}/" if prefix else ""
    if any(True for _ in client.list_blobs(bucket, prefix=f"{base}tables/", max_results=1)):
        return f"gs://{bucket}/{prefix}"  # `location` is itself a run folder
    blobs = client.list_blobs(bucket, prefix=base)
    runs = {
        b.name[len(base) :].split("/", 1)[0]
        for b in blobs
        if b.name[len(base) :].count("/") >= 2 and b.name[len(base) :].split("/")[1] == "tables"
    }
    if not runs:
        raise InputError(f"No extraction run (a folder with tables/) found in {location}")
    return f"gs://{bucket}/{base}{sorted(runs)[-1]}"


def load_run(location: str | Path, tables: Iterable[str]) -> ApiRun:
    """Read the Parquet tables (and manifest) of the run in `location`: each of `tables` that
    the run has. Whether a missing table is an error is up to the caller (`sources.yml`)."""
    names = list(dict.fromkeys(tables))
    run = find_run(location)
    if _is_gcs(run):
        with tempfile.TemporaryDirectory() as tmp:
            local = _download_gcs(run, Path(tmp))
            loaded = _read_local(local, names)
    else:
        loaded = _read_local(Path(run), names)
    return ApiRun(run, loaded[0], loaded[1])


def _download_gcs(location: str, target: Path, client=None) -> Path:
    client = client or _gcs_client()
    bucket, prefix = _split_gcs(location)
    for blob in client.list_blobs(bucket, prefix=prefix + "/"):
        rel = blob.name[len(prefix) + 1 :]
        if rel.startswith("tables/") or rel == "manifest.json":
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest))
    return target


def _read_local(run_dir: Path, names: list[str]) -> tuple[dict[str, pd.DataFrame], dict]:
    tables: dict[str, pd.DataFrame] = {}
    for name in names:
        path = run_dir / "tables" / f"{name}.parquet"
        if path.exists():
            tables[name] = pd.read_parquet(path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    return tables, manifest


# ---- the seeds ----------------------------------------------------------------------


def _read_csv(path: Path, required: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise InputError(f"{path.name} is missing columns: {', '.join(missing)}")
    return df


def load_aliases(path: Path) -> pd.DataFrame:
    """Read the item alias file (sales item name -> current item name / category)."""
    if not path.exists():
        return pd.DataFrame(columns=ALIAS_COLUMNS)
    df = _read_csv(path, ALIAS_COLUMNS)
    for column in ALIAS_COLUMNS:
        df[column] = df[column].str.strip()
    duplicated = df.loc[df["sales_item"].duplicated(), "sales_item"].tolist()
    if duplicated:
        raise InputError(f"{path.name}: sales_item listed more than once: {duplicated}")
    return df


def load_category_groups(path: Path) -> pd.DataFrame:
    """Read the category group file (adjusted category -> level 1 and optional level 2 group)."""
    if not path.exists():
        return pd.DataFrame(columns=CATEGORY_GROUP_COLUMNS)
    df = _read_csv(path, CATEGORY_GROUP_COLUMNS)
    for column in CATEGORY_GROUP_COLUMNS:
        df[column] = df[column].str.strip()
    df["category_group_level_1"] = df["category_group_level_1"].str.lower()
    if (df["category"] == "").any():
        raise InputError(f"{path.name}: a row has an empty category")
    duplicated = df.loc[df["category"].duplicated(), "category"].tolist()
    if duplicated:
        raise InputError(f"{path.name}: category listed more than once: {duplicated}")
    invalid = df.loc[
        ~df["category_group_level_1"].isin(CATEGORY_GROUPS_LEVEL_1), "category_group_level_1"
    ]
    if not invalid.empty:
        raise InputError(
            f"{path.name}: category_group_level_1 must be one of "
            f"{CATEGORY_GROUPS_LEVEL_1}, got {sorted(set(invalid))}"
        )
    return df


def load_calendar_events(path: Path) -> pd.DataFrame:
    """Read the calendar events (holidays, closures, promotions), one row per date range.

    `date_to` may be empty for a single day. Ranges may overlap (a holiday on which the cafe
    is closed is two rows).
    """
    if not path.exists():
        return pd.DataFrame(columns=CALENDAR_EVENT_COLUMNS)
    df = _read_csv(path, CALENDAR_EVENT_COLUMNS)
    for column in CALENDAR_EVENT_COLUMNS:
        df[column] = df[column].str.strip()
    df["kind"] = df["kind"].str.lower()
    df["date_to"] = df["date_to"].where(df["date_to"] != "", df["date_from"])
    for column in ["date_from", "date_to"]:
        parsed = pd.to_datetime(df[column], format="%Y-%m-%d", errors="coerce")
        if parsed.isna().any():
            bad = df.loc[parsed.isna(), column].tolist()[:5]
            raise InputError(f"{path.name}: '{column}' must be a date (YYYY-MM-DD), got {bad}")
        df[column] = parsed.dt.date
    backwards = df.loc[df["date_to"] < df["date_from"], "name"].tolist()
    if backwards:
        raise InputError(f"{path.name}: date_to is before date_from for {backwards}")
    invalid = sorted(set(df.loc[~df["kind"].isin(CALENDAR_EVENT_KINDS), "kind"]))
    if invalid:
        raise InputError(f"{path.name}: kind must be one of {CALENDAR_EVENT_KINDS}, got {invalid}")
    if (df["name"] == "").any():
        raise InputError(f"{path.name}: a row has an empty name")
    duplicated = df.loc[df.duplicated(["date_from", "kind", "name"]), "name"].tolist()
    if duplicated:
        raise InputError(f"{path.name}: event listed more than once: {duplicated}")
    return df
