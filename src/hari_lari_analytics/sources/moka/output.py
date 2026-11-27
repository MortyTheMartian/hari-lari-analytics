"""The files of a run in its staging folder, before `core.landing.land` writes them."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_run(staging: Path, tables: dict[str, pd.DataFrame], request_log: list[list]) -> None:
    (staging / "tables").mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(staging / "tables" / f"{name}.parquet", index=False)
    log = pd.DataFrame(
        request_log, columns=["time", "path", "status", "bytes", "seconds", "retry_after"]
    )
    log.to_csv(staging / "request_log.csv", index=False)
