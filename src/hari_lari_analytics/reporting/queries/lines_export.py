"""The cleaned sales lines for the Excel export, up to the anchor.

The columns are those of the Excel file (`report_excel.COLUMNS`): no personal data and never
the line note, which is free text and may contain names.
"""

from __future__ import annotations

from hari_lari_analytics.report_excel import COLUMNS
from hari_lari_analytics.reporting.queries import NoOptions, QueryContext, query
from hari_lari_analytics.reporting.result import Result, ResultMeta

EXPORT_COLUMNS = [c for c, *_ in COLUMNS]


@query("lines_export")
def lines_export(ctx: QueryContext, options: NoOptions) -> Result:
    df = ctx.con.execute(
        f"select {', '.join(EXPORT_COLUMNS)} from fct_sales_lines order by sale_ts, receipt_nr"
    ).df()
    return Result(df, ResultMeta(shape="rows", window=None, empty=df.empty))
