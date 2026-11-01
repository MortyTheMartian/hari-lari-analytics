"""Excel export: cleaned sales lines as a filterable table, ready for pivoting."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# (column, header, number format, description)
COLUMNS = [
    ("receipt_nr", "Receipt Nr", None, "Moka receipt number. One receipt = one order."),
    ("sale_ts", "Sale Timestamp", "yyyy-mm-dd hh:mm:ss", "Date and time of the line (local time)."),
    ("sale_hour", "Sale Date-Hour", "yyyy-mm-dd hh:00", "Timestamp truncated to the hour."),
    ("sale_date", "Sale Date", "yyyy-mm-dd", "Calendar date."),
    ("sale_week", "Sale Week", "yyyy-mm-dd", "Monday of the ISO week."),
    ("sale_iso_week", "Sale ISO Week", None, "ISO week label, e.g. 2026-W44 (ISO year)."),
    ("sale_month", "Sale Month", "yyyy-mm", "First day of the month."),
    ("sale_quarter", "Sale Quarter", None, "Calendar quarter, e.g. 2026-Q4."),
    ("sale_year", "Sale Year", "0", "Calendar year."),
    ("hour_of_day", "Hour of Day", "0", "Hour of the day, 0–23."),
    ("event_type", "Event Type", None, "Payment or Refund."),
    ("refund_reason", "Refund Reason", None, "Reason given in Moka for refund lines."),
    (
        "counts_as_order",
        "Counts As Order",
        None,
        "FALSE for every line of a fully refunded receipt; those receipts are not orders.",
    ),
    ("category", "Category", None, "Category as recorded in Moka at the time of sale."),
    (
        "adjusted_category",
        "Adjusted Category",
        None,
        "Category according to today's item library (or config/item_aliases.csv).",
    ),
    ("item", "Item", None, "Item name as recorded at the time of sale."),
    ("adjusted_item", "Adjusted Item", None, "Today's name of the item."),
    ("variant", "Variant", None, "Variant as recorded (Hot / Cold), capitalization normalized."),
    ("item_variant", "Item-Variant", None, "Item and variant joined with '-', or just Item."),
    ("adjusted_item_variant", "Adjusted Item-Variant", None, "Adjusted Item joined with Variant."),
    ("discounted", "Discounted", None, "TRUE if a discount was applied to the receipt."),
    ("discount_code", "Discount Code", None, "Name of the applied discount, empty if none."),
    ("payment_type", "Payment Type", None, "Payment method of the receipt."),
    ("quantity", "Quantity", "#,##0", "Units on the line; refund lines are negative."),
    ("gross_sales", "Gross Sales IDR", "#,##0", "Price × quantity before discounts."),
    ("discounts", "Discounts IDR", "#,##0", "Discount amount (positive number)."),
    ("refunds", "Refunds IDR", "#,##0", "Refunded amount (positive number)."),
    ("net_sales", "Net Sales IDR", "#,##0", "Gross Sales − Discounts − Refunds."),
]

PIVOT_TIPS = [
    "Gross / Discounts / Refunds / Net Sales IDR, Items Sold (= Quantity): use Sum.",
    "Orders: tick 'Add this data to the Data Model' when inserting the PivotTable, filter "
    "'Counts As Order' = TRUE, then use 'Distinct Count' of Receipt Nr. Never sum orders "
    "across categories or items: one receipt can contain several.",
    "Average Order Value = Net Sales IDR / Orders.",
    "% Category Order Penetration = orders containing the category / all orders in the same "
    "filter. It is easiest to read in the PDF report.",
]


def write_excel(path: Path, lines: pd.DataFrame) -> None:
    columns = [c for c, *_ in COLUMNS]
    data = lines[columns].rename(columns={c: h for c, h, *_ in COLUMNS})
    data = data.sort_values(["Sale Timestamp", "Receipt Nr"]).reset_index(drop=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        data.to_excel(writer, sheet_name="Sales Lines", index=False)
        ws = writer.sheets["Sales Lines"]
        last = f"{get_column_letter(len(COLUMNS))}{len(data) + 1}"
        table = Table(displayName="SalesLines", ref=f"A1:{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium7", showRowStripes=True)
        ws.add_table(table)
        ws.freeze_panes = "B2"
        for index, (_, header, number_format, _) in enumerate(COLUMNS, start=1):
            letter = get_column_letter(index)
            ws.column_dimensions[letter].width = max(12, len(header) + 4)
            if number_format:
                for (cell,) in ws.iter_rows(min_row=2, min_col=index, max_col=index):
                    cell.number_format = number_format

        guide = pd.DataFrame([(h, d) for _, h, _, d in COLUMNS], columns=["Column", "Description"])
        guide.to_excel(writer, sheet_name="Column Guide", index=False)
        tips = pd.DataFrame({"How to pivot": PIVOT_TIPS})
        tips.to_excel(writer, sheet_name="Column Guide", index=False, startrow=len(guide) + 2)
        guide_ws = writer.sheets["Column Guide"]
        guide_ws.column_dimensions["A"].width = 28
        guide_ws.column_dimensions["B"].width = 90
