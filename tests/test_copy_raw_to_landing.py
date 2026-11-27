"""scripts/copy_raw_to_landing.py (plan t12): an old extraction run becomes a landing run."""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from hari_lari_analytics.core import landing
from hari_lari_analytics.warehouse.db import open_run

from .api_fixture import api_tables
from .conftest import FIXTURES

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "copy_raw_to_landing.py"
spec = importlib.util.spec_from_file_location("copy_raw_to_landing", SCRIPT)
copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy)


def _old_run(root: Path, day: str, **manifest) -> Path:
    run = root / day
    (run / "tables").mkdir(parents=True)
    (run / "raw").mkdir()
    for name, df in api_tables().items():
        df.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    (run / "raw" / "transactions.json").write_text('{"payments": []}')
    (run / "request_log.csv").write_text("time,path\n")
    (run / "manifest.json").write_text(json.dumps({
        "run_at": f"{day}T20:55:16+08:00", "since": "2026-01-01", "until": None,
        "row_counts": {n: len(df) for n, df in api_tables().items()},
        "http_calls": 23, "duration_seconds": 121.8,
        "token_expires_at": "2027-05-21T14:55:51+08:00", "token_refreshed": False,
        "errors": [], "warnings": ["last sale is 4 days old"], **manifest,
    }))  # fmt: skip
    return run


def test_old_runs_are_copied_with_a_landing_manifest(tmp_path, capsys):
    _old_run(tmp_path / "raw", "2026-10-14")
    (tmp_path / "raw" / "2026-10-15" / "tables").mkdir(parents=True)  # no manifest: not a run
    assert copy.main(["--src", str(tmp_path / "raw"), "--dest", str(tmp_path / "landing")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2026-10-14: copied to") and "transaction_lines 9" in out

    run = tmp_path / "landing" / "moka" / "2026-10-14"
    manifest = json.loads((run / "manifest.json").read_text())
    jsonschema.validate(manifest, json.loads(landing.schema_path().read_text()))
    assert manifest["run_id"] == "205516-copy"
    assert manifest["finished_at"] == "2026-10-14T20:57:17.800000+08:00"
    assert manifest["tables"]["items"]["kind"] == "snapshot"
    assert manifest["tables"]["transactions"]["latest_at"] == "2026-10-13T11:00:00"
    assert manifest["checks"]["warnings"] == ["last sale is 4 days old"]
    assert manifest["source_info"]["copied_from"] == str(tmp_path / "raw" / "2026-10-14")
    with gzip.open(run / "raw" / "transactions.json.gz", "rt") as f:
        assert json.load(f) == {"payments": []}
    assert not (run / "raw" / "transactions.json").exists()
    db = open_run(run, FIXTURES)
    assert db.con.execute("select count(*) from fct_sales_lines").fetchone()[0] > 0


def test_a_date_that_landed_is_skipped_unless_forced(tmp_path, capsys):
    _old_run(tmp_path / "raw", "2026-10-14")
    args = ["--src", str(tmp_path / "raw"), "--dest", str(tmp_path / "landing")]
    copy.main(args)
    capsys.readouterr()
    copy.main(args)
    assert "already landed" in capsys.readouterr().out
    copy.main([*args, "--force"])
    assert "copied to" in capsys.readouterr().out


def test_row_counts_that_differ_from_the_old_manifest_fail(tmp_path):
    _old_run(tmp_path / "raw", "2026-10-14", row_counts={"transactions": 1})
    with pytest.raises(ValueError, match="transactions has 6 rows, its manifest says 1"):
        copy.main(["--src", str(tmp_path / "raw"), "--dest", str(tmp_path / "landing")])
