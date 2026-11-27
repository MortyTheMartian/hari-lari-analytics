"""The suite does without the pandas pipeline (plan t5), so it can be deleted (t6): this test
runs after all the others (`conftest.py` puts it last) and finds none of it imported. Run on
its own it proves nothing."""

import sys

from hari_lari_analytics import load_api

PANDAS_MODULES = [
    "hari_lari_analytics.clean",
    "hari_lari_analytics.dimensions",
    "hari_lari_analytics.metrics",
]
# the mapping imports `clean` when it runs; imported by name it shows up in a test module
PANDAS_MAPPING = [load_api.api_sales_lines, load_api.load_api_sales]


def test_no_test_imported_the_pandas_pipeline():
    assert [m for m in PANDAS_MODULES if m in sys.modules] == []
    holders = [
        f"{name}.{attr}"
        for name, module in list(sys.modules.items())
        if name == "tests" or name.startswith("tests.")
        for attr, value in vars(module).items()
        if any(value is f for f in PANDAS_MAPPING)
    ]
    assert holders == []
