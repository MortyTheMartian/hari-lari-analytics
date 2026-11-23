from openpyxl import load_workbook

from hari_lari_analytics.cli import main

from .api_fixture import api_tables
from .conftest import FIXTURES


def test_writes_pdf_and_excel(tmp_path, capsys):
    exit_code = main(
        [
            "--input-dir",
            str(FIXTURES),
            "--output-dir",
            str(tmp_path),
            "--aliases",
            str(FIXTURES / "item_aliases.csv"),
        ]
    )
    assert exit_code == 0
    pdfs = list(tmp_path.glob("*/hari_lari_sales_report_2026-10-13.pdf"))
    workbooks = list(tmp_path.glob("*/hari_lari_sales_lines_2026-10-13.xlsx"))
    covers = list(tmp_path.glob("*/hari_lari_sales_cover_2026-10-13.png"))
    assert len(pdfs) == len(workbooks) == len(covers) == 1
    assert pdfs[0].read_bytes().startswith(b"%PDF")
    assert covers[0].read_bytes().startswith(b"\x89PNG")

    sheet = load_workbook(workbooks[0])["Sales Lines"]
    assert sheet.max_row == 10
    assert "Customer" not in [cell.value for cell in sheet[1]]
    assert "Mystery Cake" in capsys.readouterr().out


def test_missing_inputs_exit_with_error(tmp_path, capsys):
    assert main(["--input-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")]) == 1
    assert "no sales report" in capsys.readouterr().err


def _api_args(tmp_path, *extra):
    run = tmp_path / "extract" / "2026-10-14"
    (run / "tables").mkdir(parents=True)
    for name, table in api_tables().items():
        table.to_parquet(run / "tables" / f"{name}.parquet", index=False)
    return [
        "--source", "api",
        "--extract-dir", str(tmp_path / "extract"),
        "--output-dir", str(tmp_path / "out"),
        "--aliases", str(FIXTURES / "item_aliases.csv"),
        *extra,
    ]  # fmt: skip


def test_api_source_writes_the_same_files(tmp_path, capsys):
    assert main(_api_args(tmp_path, "--anchor", "2026-10-13")) == 0
    out = tmp_path / "out"
    assert len(list(out.glob("*/hari_lari_sales_report_2026-10-13.pdf"))) == 1
    assert len(list(out.glob("*/hari_lari_sales_cover_2026-10-13.png"))) == 1
    workbook = next(out.glob("*/hari_lari_sales_lines_2026-10-13.xlsx"))
    sheet = load_workbook(workbook)["Sales Lines"]
    assert sheet.max_row == 10
    headers = [cell.value for cell in sheet[1]]
    options = [row[headers.index("Modifier Option")].value for row in sheet.iter_rows(min_row=2)]
    assert options.count("Chicken + Poached") == 1
    assert options.count(None) == 8
    printed = capsys.readouterr().out
    assert "Mystery Cake" in printed
    assert "3 orders" not in printed  # R003 is fully refunded: 4 orders of 5 receipts
    assert "4 orders" in printed


def test_anchor_ignores_later_sales(tmp_path, capsys):
    assert main(_api_args(tmp_path, "--anchor", "2026-10-05")) == 0
    out = tmp_path / "out"
    assert len(list(out.glob("*/hari_lari_sales_report_2026-10-05.pdf"))) == 1
    sheet = load_workbook(next(out.glob("*/*.xlsx")))["Sales Lines"]
    assert sheet.max_row == 4  # three lines of 5 Oct
    assert "2 orders" in capsys.readouterr().out


def test_anchor_before_the_first_sale_is_an_error(tmp_path, capsys):
    assert main(_api_args(tmp_path, "--anchor", "2026-01-01")) == 1
    assert "no sales up to" in capsys.readouterr().err


def test_api_source_without_a_run_exits_with_error(tmp_path, capsys):
    assert main(["--source", "api", "--extract-dir", str(tmp_path)]) == 1
    assert "No extraction run" in capsys.readouterr().err
