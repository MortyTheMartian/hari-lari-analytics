"""Write the golden files next to this script from the pandas pipeline (plan t5).

    uv run python -m tests.golden.make_golden

It needs `clean.py`, `dimensions.py`, `metrics.py` and the pandas mapping of `load_api.py`, so
it goes when they are deleted (t6); the golden files stay.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from hari_lari_analytics.dimensions import prepare_lines
from hari_lari_analytics.load_api import api_sales_lines
from hari_lari_analytics.metrics import compute_metrics, contributive_aov
from hari_lari_analytics.periods import report_periods
from hari_lari_analytics.project import use_project

from . import GOLDEN, PERIOD_ANCHORS, as_text, text
from .cases import FIXTURES, LAST_SALE, LINE_CASES, Case

METRICS = ["gross_sales", "discounts", "refunds", "net_sales", "orders", "items_sold", "aov",
           "first_sale_ts", "last_sale_ts"]  # fmt: skip
CATEGORY = ["net_sales", "items_sold", "orders", "penetration", "category_aov",
            "contributive_aov"]  # fmt: skip


def pandas_lines(case: Case) -> pd.DataFrame:
    with use_project(case.settings):
        result = api_sales_lines(case.tables(), case.item_aliases())
        lines, _ = prepare_lines(result, FIXTURES / "category_group.csv", case.anchor)
    return lines


def _periods() -> pd.DataFrame:
    rows = [
        {
            "anchor": anchor.isoformat(),
            "key": p.key,
            "title": p.title,
            "label": p.label(),
            "prior_label": p.prior_label(),
            "start": p.start.isoformat(),
            "end": p.end.isoformat(),
            "prior_start": p.prior_start.isoformat(),
            "prior_end": p.prior_end.isoformat(),
        }  # fmt: skip
        for anchor in PERIOD_ANCHORS
        for p in report_periods(anchor)
    ]
    return pd.DataFrame(rows)


def _scopes(lines: pd.DataFrame) -> dict[str, tuple[pd.DataFrame, bool]]:
    """The lines of the full history and of each report period, with whether the period has a
    comparison (its prior period starts on or after the first sale)."""
    first = lines["sale_ts"].min().date()
    scopes = {"full": (lines, False)}
    for p in report_periods(LAST_SALE):
        scopes[p.key] = (lines[p.mask(lines["sale_ts"])], p.prior_start >= first)
    return scopes


def _metrics(lines: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for scope, (part, compare_ok) in _scopes(lines).items():
        totals = compute_metrics(part).iloc[0][METRICS].to_dict()
        trading_days = part.loc[part["counts_as_order"], "sale_date"].nunique()
        rows.append({"scope": scope, **totals, "trading_days": trading_days,
                     "compare_ok": compare_ok, "empty": part.empty})  # fmt: skip
    return pd.DataFrame(rows)


def _by_category(lines: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for scope, (part, _) in _scopes(lines).items():
        cats = contributive_aov(part)[["adjusted_category", *CATEGORY]]
        parts.append(cats.assign(scope=scope)[["scope", "adjusted_category", *CATEGORY]])
    return pd.concat(parts, ignore_index=True)


def _by_payment_type(lines: pd.DataFrame) -> pd.DataFrame:
    return compute_metrics(lines, ["payment_type"])[["payment_type", *METRICS]]


def _write(df: pd.DataFrame, path: Path, sort: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = as_text(df) if sort else df.map(text)
    out.to_csv(path, index=False)
    print(f"{path.relative_to(GOLDEN)}: {len(out)} rows, {len(out.columns)} columns")


def main() -> None:
    for name, case in LINE_CASES.items():
        _write(pandas_lines(case), GOLDEN / "lines" / f"{name}.csv")
    _write(_periods(), GOLDEN / "periods.csv", sort=False)
    lines = pandas_lines(LINE_CASES["fixture"])
    _write(_metrics(lines), GOLDEN / "metrics.csv", sort=False)
    _write(_by_category(lines), GOLDEN / "metrics_by_category.csv", sort=False)
    _write(_by_payment_type(lines), GOLDEN / "metrics_by_payment_type.csv", sort=False)


if __name__ == "__main__":
    main()
