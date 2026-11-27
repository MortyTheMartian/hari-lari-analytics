"""Finding and reading an extraction run. What the models make of its tables is checked in
`test_models.py`, against the golden lines (`tests/golden/`)."""

from pathlib import Path

import pandas as pd
import pytest

from hari_lari_analytics.reporting.db import open_run
from hari_lari_analytics.reporting.inputs import InputError, find_run, load_run

from .api_fixture import api_tables
from .conftest import FIXTURES


def _write_run(root: Path, name: str, tables: dict[str, pd.DataFrame]) -> Path:
    run = root / name
    (run / "tables").mkdir(parents=True)
    for table, df in tables.items():
        df.to_parquet(run / "tables" / f"{table}.parquet", index=False)
    (run / "manifest.json").write_text('{"last_sale": "2026-10-13T11:00:00"}')
    return run


def test_load_run_takes_the_latest_dated_run(tmp_path):
    _write_run(tmp_path, "2026-10-12", api_tables())
    latest = _write_run(tmp_path, "2026-10-14", api_tables())
    assert find_run(tmp_path) == str(latest)
    assert find_run(latest) == str(latest)

    run = load_run(tmp_path, api_tables())
    assert run.name == "2026-10-14"
    assert run.manifest["last_sale"] == "2026-10-13T11:00:00"
    assert set(run.tables) == set(api_tables())
    assert len(run.tables["transaction_lines"]) == 9


def test_load_run_reads_only_the_tables_asked_for_that_the_run_has(tmp_path):
    run = load_run(_write_run(tmp_path, "2026-10-14", api_tables()), ["items", "no_such_table"])
    assert set(run.tables) == {"items"}


def test_load_run_without_a_run_is_an_input_error(tmp_path):
    with pytest.raises(InputError, match="No extraction run"):
        find_run(tmp_path)


def test_a_run_without_a_required_table_is_an_input_error(tmp_path):
    tables = api_tables()
    del tables["items"]
    run = _write_run(tmp_path, "2026-10-14", tables)
    with pytest.raises(InputError, match="no table items"):
        open_run(run, FIXTURES)
