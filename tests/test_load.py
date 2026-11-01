import shutil

import pytest

from hari_lari_analytics.load import InputError, find_inputs, load_sales

from .conftest import FIXTURES

SALES = "2026_10_14_sales_report_item_details_01-10-2026_13-10-2026.csv"
LIBRARY = "2026_10_14_item_library.csv"


def test_finds_exactly_one_file_of_each_kind():
    library, sales = find_inputs(FIXTURES)
    assert (library.name, sales.name) == (LIBRARY, SALES)


def test_missing_files_are_reported(tmp_path):
    with pytest.raises(InputError, match="no item library.*no sales report"):
        find_inputs(tmp_path)


def test_two_sales_reports_are_rejected(tmp_path):
    shutil.copy(FIXTURES / LIBRARY, tmp_path / LIBRARY)
    shutil.copy(FIXTURES / SALES, tmp_path / SALES)
    shutil.copy(
        FIXTURES / SALES,
        tmp_path / "2026_10_15_sales_report_item_details_01-10-2026_14-10-2026.csv",
    )
    with pytest.raises(InputError, match="more than one sales report"):
        find_inputs(tmp_path)


def test_amounts_are_whole_rupiah_integers():
    sales = load_sales(FIXTURES / SALES)
    assert sales["Net Sales"].dtype == "int64"
    assert sales["Quantity"].tolist()[4] == -1


def test_unreconciled_amounts_are_rejected(tmp_path):
    text = (
        (FIXTURES / SALES).read_text().replace(",35000.0,0.0,0.0,35000.0,", ",35000.0,0.0,0.0,1.0,")
    )
    broken = tmp_path / SALES
    broken.write_text(text)
    with pytest.raises(InputError, match="Gross - Discounts - Refunds != Net Sales"):
        load_sales(broken)
