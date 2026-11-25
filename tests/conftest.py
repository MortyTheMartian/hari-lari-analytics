from pathlib import Path

import pytest

from hari_lari_analytics.clean import clean_sales
from hari_lari_analytics.dimensions import add_dimensions
from hari_lari_analytics.load import find_inputs, load_aliases, load_library, load_sales

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        help="rewrite tests/snapshots/ from the current results of the components",
    )


@pytest.fixture
def cleaned():
    library_path, sales_path = find_inputs(FIXTURES)
    return clean_sales(
        load_sales(sales_path),
        load_library(library_path),
        load_aliases(FIXTURES / "item_aliases.csv"),
    )


@pytest.fixture
def lines(cleaned):
    return add_dimensions(cleaned.lines)


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """The fixture run as an extraction run on disk: `tables/<name>.parquet`."""
    from .api_fixture import api_tables

    run = tmp_path_factory.mktemp("extract") / "2026-10-14"
    (run / "tables").mkdir(parents=True)
    for name, table in api_tables().items():
        table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    return run
