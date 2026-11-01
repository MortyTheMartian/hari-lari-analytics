from openpyxl import load_workbook

from hari_lari_analytics.cli import main

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
    assert len(pdfs) == len(workbooks) == 1
    assert pdfs[0].read_bytes().startswith(b"%PDF")

    sheet = load_workbook(workbooks[0])["Sales Lines"]
    assert sheet.max_row == 10
    assert "Customer" not in [cell.value for cell in sheet[1]]
    assert "Mystery Cake" in capsys.readouterr().out


def test_missing_inputs_exit_with_error(tmp_path, capsys):
    assert main(["--input-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")]) == 1
    assert "no sales report" in capsys.readouterr().err
