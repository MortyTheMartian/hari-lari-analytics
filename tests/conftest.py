from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        help="rewrite tests/snapshots/ from the current results of the components",
    )


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """The fixture run as an extraction run on disk: `tables/<name>.parquet`."""
    from .api_fixture import api_tables

    run = tmp_path_factory.mktemp("extract") / "2026-10-14"
    (run / "tables").mkdir(parents=True)
    for name, table in api_tables().items():
        table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    return run


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    """The fixture run landed and built into a stored warehouse (run date 14 Oct 2026, anchor
    13 Oct): the path of its `warehouse.duckdb`."""
    from datetime import date

    from hari_lari_analytics.core.store import LocalStore
    from hari_lari_analytics.warehouse.build import build_warehouse

    from .api_fixture import land_fixture

    root = LocalStore(tmp_path_factory.mktemp("data"))
    land_fixture(root.child("landing"), date(2026, 10, 14))
    info = build_warehouse(root, date(2026, 10, 14), str(FIXTURES))
    return root.root / info.database
