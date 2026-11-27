import json
import shutil
from datetime import date, datetime

import duckdb
import pandas as pd
import pytest

from hari_lari_analytics.core.config import ConfigError
from hari_lari_analytics.core.store import LocalStore
from hari_lari_analytics.orchestration import warehouse_cli
from hari_lari_analytics.warehouse.build import (
    LATEST,
    build_warehouse,
    open_warehouse,
    read_build,
    read_latest,
)
from hari_lari_analytics.warehouse.checks import DataCheckError, run_checks
from hari_lari_analytics.warehouse.db import Seeds, connect
from hari_lari_analytics.warehouse.inputs import InputError

from .api_fixture import EXTRACTED_AT, api_tables, land_fixture
from .conftest import FIXTURES

RUN_DATE = date(2026, 10, 14)
BUILT_AT = datetime(2026, 10, 14, 4, 5)


@pytest.fixture
def root(tmp_path):
    store = LocalStore(tmp_path / "data")
    land_fixture(store.child("landing"), RUN_DATE)
    return store


def build(root, run_date=RUN_DATE, config=FIXTURES):
    return build_warehouse(root, run_date, str(config), now=BUILT_AT)


def in_memory(anchor):
    return connect(api_tables(), Seeds.from_dir(FIXTURES), anchor, extracted_at=EXTRACTED_AT)


def test_a_build_writes_the_database_the_marts_and_latest_json(root):
    info = build(root)
    folder = root.root / "warehouse" / "2026-10-14"
    assert sorted(p.name for p in folder.iterdir()) == ["build.json", "marts", "warehouse.duckdb"]
    assert sorted(p.name for p in (folder / "marts").iterdir()) == [
        "dim_calendar.parquet", "dim_categories.parquet", "dim_items.parquet",
        "fct_orders.parquet", "fct_sales_lines.parquet", "fct_stock.parquet",
    ]  # fmt: skip
    assert info.anchor == date(2026, 10, 13)  # project.yml: yesterday
    assert info.database == "warehouse/2026-10-14/warehouse.duckdb"
    assert info.landing["moka"].run_id == "093000-0000"
    assert sorted(info.config) == ["calendar_events.csv", "category_group.csv", "item_aliases.csv"]
    assert read_latest(root) == info == read_build(root, "2026-10-14")


def test_the_database_and_the_marts_match_the_in_memory_build(root):
    info = build(root)
    memory = in_memory(info.anchor)
    stored = open_warehouse(root.root / info.database)
    parquet = duckdb.connect()
    for model, rows in info.marts.items():
        mart = f"'{root.root}/warehouse/2026-10-14/marts/{model}.parquet'"
        counts = [
            con.execute(f"select count(*) from {table}").fetchone()[0]
            for con, table in [(memory, model), (stored.con, model), (parquet, mart)]
        ]
        assert counts == [rows] * 3, model
    net = "select sum(net_sales) from {}"
    mart = f"'{root.root}/warehouse/2026-10-14/marts/fct_sales_lines.parquet'"
    assert (
        memory.execute(net.format("fct_sales_lines")).fetchone()
        == stored.con.execute(net.format("fct_sales_lines")).fetchone()
        == parquet.execute(net.format(mart)).fetchone()
    )


def test_a_mart_leaves_out_the_private_columns(root):
    build(root)
    mart = root.root / "warehouse" / "2026-10-14" / "marts" / "fct_sales_lines.parquet"
    assert "note" not in pd.read_parquet(mart).columns
    stored = open_warehouse(root.root / "warehouse" / "2026-10-14" / "warehouse.duckdb")
    notes = stored.con.execute("select note from fct_sales_lines where note is not null")
    assert notes.fetchall() == [("oat milk please",)]


def test_the_stored_warehouse_keeps_the_run_and_the_check_results(root):
    land_fixture(root.child("landing"), date(2026, 10, 15), warnings=["2 lines without a price"])
    info = build(root, date(2026, 10, 15))
    stored = open_warehouse(root.root / info.database)
    assert stored.run.name == "2026-10-15"
    assert stored.run.check_messages("warnings") == ["2 lines without a price"]
    assert stored.con.execute("select extracted_at from run_context").fetchone() == (EXTRACTED_AT,)
    memory = run_checks(in_memory(info.anchor))
    assert len(stored.checks.results) == len(memory.results) == info.checks.checks
    stored_warnings = [r.describe() for r in stored.checks.warnings]
    assert stored_warnings == [r.describe() for r in memory.warnings] == info.checks.warnings


def test_another_anchor_rebuilds_the_models_in_memory(root):
    info = build(root)
    stored = open_warehouse(root.root / info.database, anchor=date(2026, 10, 12))
    memory = in_memory(date(2026, 10, 12))
    query = "select count(*), sum(net_sales) from fct_sales_lines"
    assert stored.con.execute(query).fetchone() == memory.execute(query).fetchone()
    assert stored.con.execute("select anchor from run_context").fetchone() == (date(2026, 10, 12),)
    assert stored.con.execute("select source from warehouse_landing").fetchall() == [("moka",)]


def test_the_newest_landing_run_up_to_the_date_is_read(root):
    land_fixture(root.child("landing"), date(2026, 10, 20))
    assert build(root, date(2026, 10, 16)).landing["moka"].run_date == RUN_DATE
    assert build(root, date(2026, 10, 20)).landing["moka"].run_date == date(2026, 10, 20)


def test_latest_json_never_goes_back_to_an_older_date(root):
    build(root, date(2026, 10, 20))
    build(root, date(2026, 10, 16))
    assert read_latest(root).date == date(2026, 10, 20)
    assert read_build(root, "2026-10-16").date == date(2026, 10, 16)


def test_without_a_landing_run_nothing_is_built(root):
    with pytest.raises(InputError, match="No landing run of moka on or before 2026-10-13"):
        build(root, date(2026, 10, 13))
    assert not (root.root / "warehouse").exists()


def test_a_failing_error_check_writes_nothing(root, tmp_path):
    tables = api_tables()
    tables["transactions"] = pd.concat([tables["transactions"]] * 2)  # duplicate payment ids
    land_fixture(root.child("landing"), date(2026, 10, 15), tables=tables)
    with pytest.raises(DataCheckError, match="stg_transactions: payment_id is unique"):
        build(root, date(2026, 10, 15))
    assert not (root.root / "warehouse").exists()


def test_a_missing_config_file_fails_the_build(root, tmp_path):
    config = tmp_path / "config"
    shutil.copytree(FIXTURES, config, ignore=shutil.ignore_patterns("calendar_events.csv"))
    with pytest.raises(ConfigError, match="has no calendar_events.csv"):
        build(root, config=config)
    assert not (root.root / "warehouse").exists()


def test_the_cli_builds_and_a_dry_run_writes_under_dry_run(root, capsys):
    args = ["build", "--date", "2026-10-14", "--data-root", str(root.root),
            "--config", str(FIXTURES)]  # fmt: skip
    assert warehouse_cli.main([*args, "--dry-run"]) == 0
    assert not root.exists(LATEST)
    dry = json.loads((root.root / "dry-run" / LATEST).read_text())
    assert dry["date"] == "2026-10-14" and dry["landing"]["moka"]["run_date"] == "2026-10-14"
    assert warehouse_cli.main(args) == 0
    out = capsys.readouterr().out
    assert "warehouse 2026-10-14 (anchor 2026-10-13) from moka 2026-10-14 (093000-0000)" in out
    assert root.exists(LATEST)
    assert warehouse_cli.main([*args[:1], "--date", "2026-10-01", *args[3:]]) == 1
    assert "No landing run of moka" in capsys.readouterr().err
