from datetime import date

import pandas as pd
import pytest

from hari_lari_analytics.periods import Period, last_3_months, last_6_weeks, last_14_days
from hari_lari_analytics.report_data import build_section

FIRST_SALE = date(2026, 10, 5)


def test_week_over_week_growth_per_bucket(lines):
    trend = build_section(lines, last_6_weeks(date(2026, 10, 18)), FIRST_SALE).trend
    assert list(trend["net_sales"].iloc[-2:]) == [98_000, 115_000]
    assert trend["net_sales_growth"].iloc[-1] == pytest.approx(115_000 / 98_000 - 1)
    assert trend["orders_growth"].iloc[-1] == 0
    # weeks without sales give no growth rather than infinity
    assert trend["net_sales_growth"].iloc[:-1].isna().all()


@pytest.mark.parametrize(
    ("first_date", "expected"),
    [(date(2026, 10, 5), 115_000 / 98_000 - 1), (date(2026, 10, 6), None)],
)
def test_first_bucket_compares_only_with_a_complete_bucket(lines, first_date, expected):
    period = Period(
        key="W",
        title="One week",
        start=date(2026, 10, 12),
        end=date(2026, 10, 18),
        grain="sale_week",
        prior_start=date(2026, 10, 5),
        prior_end=date(2026, 10, 11),
    )
    growth = build_section(lines, period, first_date).trend["net_sales_growth"].iloc[0]
    if expected is None:
        assert pd.isna(growth)
    else:
        assert growth == pytest.approx(expected)


def test_sparklines_are_weekly_blocks_ending_on_the_last_day(lines):
    weeks = build_section(lines, last_6_weeks(date(2026, 10, 18)), FIRST_SALE).spark
    assert len(weeks) == 6
    assert list(weeks["net_sales"].iloc[-2:]) == [98_000, 115_000]
    assert weeks["aov"].iloc[-1] == 115_000 / 2

    # 1 Aug – 31 Oct has 92 days: 13 full blocks, the leftover 1 Aug is left out
    months = build_section(lines, last_3_months(date(2026, 10, 31)), FIRST_SALE).spark
    assert len(months) == 13
    assert months.index[0] == pd.Timestamp("2026-08-02")
    assert months.index[-1] == pd.Timestamp("2026-10-25")
    assert months["net_sales"].sum() == 213_000


def test_daily_view_has_no_growth(lines):
    trend = build_section(lines, last_14_days(date(2026, 10, 13)), FIRST_SALE).trend
    assert len(trend) == 14
    assert trend["net_sales_growth"].isna().all()
    assert trend["orders_growth"].isna().all()


def test_no_comparison_when_prior_period_starts_before_first_sale(lines):
    section = build_section(lines, last_14_days(date(2026, 10, 13)), FIRST_SALE)
    assert not section.prior_ok
    assert section.kpis["change"].isna().all()
    assert section.trend["prior_net_sales"].isna().all()


def test_section_figures(lines):
    section = build_section(lines, last_14_days(date(2026, 10, 13)), FIRST_SALE)
    kpis = section.kpis.set_index("key")
    assert kpis.loc["net_sales", "current"] == 213_000
    assert kpis.loc["orders", "current"] == 4
    assert section.spark["net_sales"].sum() == 213_000
    assert section.categories["share"].sum() == pytest.approx(1)
    assert section.payments["share"].sum() == pytest.approx(1)
    assert section.trading_days == 3


def test_empty_section(lines):
    section = build_section(lines, last_3_months(date(2026, 10, 13)), FIRST_SALE)
    assert section.empty
    assert section.kpis.set_index("key").loc["net_sales", "current"] == 0
    assert section.hours.empty
    assert all(pd.api.types.is_float_dtype(t) for t in section.spark.dtypes)
