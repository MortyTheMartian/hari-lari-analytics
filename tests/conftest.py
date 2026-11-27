from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LAST = "test_zz_pandas_pipeline_unused.py"


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        help="rewrite tests/snapshots/ from the current results of the components",
    )


def pytest_collection_modifyitems(items):
    """The check that no test imported the pandas pipeline runs after all the others."""
    items.sort(key=lambda item: item.path.name == LAST)


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """The fixture run as an extraction run on disk: `tables/<name>.parquet`."""
    from .api_fixture import api_tables

    run = tmp_path_factory.mktemp("extract") / "2026-10-14"
    (run / "tables").mkdir(parents=True)
    for name, table in api_tables().items():
        table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    return run
