from pathlib import Path

import pytest

from hari_lari_analytics.clean import clean_sales
from hari_lari_analytics.dimensions import add_dimensions
from hari_lari_analytics.load import find_inputs, load_aliases, load_library, load_sales

FIXTURES = Path(__file__).parent / "fixtures"


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
