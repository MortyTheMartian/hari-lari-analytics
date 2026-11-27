"""Output location: a local folder or `gs://bucket/prefix`."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def write_run(
    staging: Path, tables: dict[str, pd.DataFrame], request_log: list[list], manifest: dict
) -> None:
    (staging / "tables").mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(staging / "tables" / f"{name}.parquet", index=False)
    log = pd.DataFrame(
        request_log, columns=["time", "path", "status", "bytes", "seconds", "retry_after"]
    )
    log.to_csv(staging / "request_log.csv", index=False)
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))


def is_gcs(location: str) -> bool:
    return location.startswith("gs://")


def upload_dir(local: Path, location: str, client=None) -> str:
    """Copy every file under `local` to gs://bucket/prefix/. Returns the destination URL."""
    if client is None:
        from google.cloud import storage  # needs the `gcp` extra

        client = storage.Client()
    bucket_name, _, prefix = location[len("gs://") :].partition("/")
    bucket = client.bucket(bucket_name)
    for f in sorted(p for p in local.rglob("*") if p.is_file()):
        key = "/".join(x for x in [prefix.strip("/"), f.relative_to(local).as_posix()] if x)
        bucket.blob(key).upload_from_filename(str(f))
    return location
