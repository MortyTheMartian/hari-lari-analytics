"""The cleaned sales lines for the Excel export, up to the anchor.

The columns are those of the Excel file (`COLUMNS`, which `outputs/xlsx.py` writes with their
headers, number formats and descriptions): no personal data and never the line note, which is
free text and may contain names.
"""

from __future__ import annotations

from hari_lari_analytics.reporting.queries import NoOptions, QueryContext, query
from hari_lari_analytics.reporting.result import Result, ResultMeta

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
    (
        "modifier_name",
        "Modifier",
        None,
        "Modifier group, e.g. Egg Preference. Empty for most lines; several are joined by ' + '.",
    ),
    (
        "modifier_option_name",
        "Modifier Option",
        None,
        "Chosen option, e.g. Poached or Chicken, in the same order as Modifier.",
    ),
    (
        "modifier_price",
        "Modifier Price IDR",
        "#,##0",
        "Sum of the modifier prices per unit. Already included in Gross Sales, not extra.",
    ),
    ("quantity", "Quantity", "#,##0", "Units on the line; refund lines are negative."),
    ("gross_sales", "Gross Sales IDR", "#,##0", "Price × quantity before discounts."),
    ("discounts", "Discounts IDR", "#,##0", "Discount amount (positive number)."),
    ("refunds", "Refunds IDR", "#,##0", "Refunded amount (positive number)."),
    ("net_sales", "Net Sales IDR", "#,##0", "Gross Sales − Discounts − Refunds."),
]

EXPORT_COLUMNS = [c for c, *_ in COLUMNS]


@query("lines_export")
def lines_export(ctx: QueryContext, options: NoOptions) -> Result:
    df = ctx.con.execute(
        f"select {', '.join(EXPORT_COLUMNS)} from fct_sales_lines "
        "order by sale_ts, receipt_nr, payment_id, line_uuid"
    ).df()
    return Result(df, ResultMeta(shape="rows", window=None, empty=df.empty))
