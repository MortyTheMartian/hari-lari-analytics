from datetime import date, timedelta

import pytest

from hari_lari_analytics.analysis.windows import (
    Bucket,
    Calendar,
    bucket_before,
    bucket_start,
    buckets,
    parse_window,
    resolve,
    shift_months,
)
from hari_lari_analytics.warehouse.db import connect

from . import golden
from .api_fixture import api_tables
from .test_models import LAST_SALE, SEEDS

FIRST_SALE = date(2026, 7, 23)


def _cal(anchor: date, trading: set[date] | None = None) -> Calendar:
    return Calendar(anchor, FIRST_SALE, frozenset(trading or ()))


def _ranges(w):
    return (w.current.start, w.current.end), (w.comparison.start, w.comparison.end)


# the periods of the weekly report's sections
@pytest.mark.parametrize(
    "name, anchor, current, prior",
    [
        ("L14D", date(2026, 10, 31), ("2026-10-18", "2026-10-31"), ("2026-10-04", "2026-10-17")),
        ("L6W", date(2026, 10, 31), ("2026-09-14", "2026-10-25"), ("2026-08-03", "2026-09-13")),
        ("L6W", date(2026, 10, 25), ("2026-09-14", "2026-10-25"), ("2026-08-03", "2026-09-13")),
        ("L3M", date(2026, 10, 31), ("2026-08-01", "2026-10-31"), ("2026-05-01", "2026-07-31")),
        ("L3M", date(2027, 1, 15), ("2026-10-01", "2026-12-31"), ("2026-07-01", "2026-09-30")),
    ],
)
def test_the_report_periods_resolve_as_before(name, anchor, current, prior):
    w = resolve(name, _cal(anchor))
    as_dates = tuple(tuple(map(date.fromisoformat, r)) for r in (current, prior))
    assert _ranges(w) == as_dates
    period = golden.periods(anchor)[name]
    assert (w.current.title, w.current.label) == (period.title, period.label)
    assert (w.comparison.label, w.comparison.start) == (period.prior_label, period.prior_start)


def test_long_and_short_names_are_the_same_window():
    cal = _cal(date(2026, 11, 22))
    for short, long in [("L14D", "last_n_days(14)"), ("L6W", "last_n_weeks(6)"),
                        ("L3M", "last_n_months(3)")]:  # fmt: skip
        assert resolve(long, cal) == resolve(short, cal)
    assert resolve("last_n_weeks(6)", cal).key == "L6W"


def test_default_grains_and_titles():
    cal = _cal(date(2026, 11, 22))
    seen = {
        name: (resolve(name, cal).current.grain, resolve(name, cal).current.title)
        for name in ["L14D", "L6W", "L3M", "L28D", "L1W", "L1M", "full", "WTD", "MTD"]
    }
    assert seen == {
        "L14D": ("day", "Last 14 days"),
        "L6W": ("week", "Last 6 complete weeks"),
        "L3M": ("month", "Last 3 complete months"),
        "L28D": ("day", "Last 28 days"),
        "L1W": ("week", "Last complete week"),
        "L1M": ("month", "Last complete month"),
        "full": ("month", "Full history"),
        "WTD": ("day", "Week to date"),
        "MTD": ("day", "Month to date"),
    }


def test_the_worked_example_of_the_design_note():
    w = resolve("L6W", _cal(date(2026, 11, 22)))
    assert _ranges(w) == ((date(2026, 10, 12), date(2026, 11, 22)),
                          (date(2026, 8, 31), date(2026, 10, 11)))  # fmt: skip
    assert w.compare_ok and w.compare_label == "prior 6 weeks"


def test_a_window_shorter_than_a_week_is_compared_with_the_same_weekdays():
    saturday = date(2026, 11, 21)
    one = resolve("L1D", _cal(saturday))
    assert _ranges(one) == ((saturday, saturday), (date(2026, 11, 14), date(2026, 11, 14)))
    assert one.compare_label == "same weekday last week"
    three = resolve("L3D", _cal(saturday))
    assert _ranges(three) == ((date(2026, 11, 19), saturday),
                              (date(2026, 11, 12), date(2026, 11, 14)))  # fmt: skip
    assert three.compare_label == "same days last week"
    # from a week on, the comparison is the same length directly before
    assert _ranges(resolve("L7D", _cal(saturday)))[1] == (date(2026, 11, 8), date(2026, 11, 14))


def test_week_to_date_compares_the_same_weekdays_a_week_earlier():
    wednesday = date(2026, 11, 18)
    w = resolve("WTD", _cal(wednesday))
    assert _ranges(w) == ((date(2026, 11, 16), wednesday),
                          (date(2026, 11, 9), date(2026, 11, 11)))  # fmt: skip
    monday = resolve("WTD", _cal(date(2026, 11, 16)))
    assert _ranges(monday) == ((date(2026, 11, 16),) * 2, (date(2026, 11, 9),) * 2)


def test_month_to_date_compares_the_same_days_a_month_earlier():
    w = resolve("MTD", _cal(date(2026, 11, 18)))
    assert _ranges(w) == ((date(2026, 11, 1), date(2026, 11, 18)),
                          (date(2026, 10, 1), date(2026, 10, 18)))  # fmt: skip
    assert w.compare_label == "same days last month"
    # the month before is shorter: the comparison ends on its last day
    end_of_march = resolve("MTD", _cal(date(2027, 3, 31)))
    assert _ranges(end_of_march)[1] == (date(2027, 2, 1), date(2027, 2, 28))


def test_full_history_has_no_valid_comparison():
    w = resolve("full", _cal(date(2026, 11, 22)))
    assert (w.current.start, w.current.end) == (FIRST_SALE, date(2026, 11, 22))
    assert w.comparison.end == FIRST_SALE - timedelta(days=1)
    assert not w.compare_ok


def test_last_year_lines_up_weekdays_or_months():
    w = resolve("L6W", _cal(date(2027, 11, 21)), compare="last_year")
    assert _ranges(w) == ((date(2027, 10, 11), date(2027, 11, 21)),
                          (date(2026, 10, 12), date(2026, 11, 22)))  # fmt: skip
    assert w.comparison.start.weekday() == w.current.start.weekday() == 0
    assert w.compare_ok and w.compare_label == "same period last year"
    months = resolve("L3M", _cal(date(2027, 10, 31)), compare="last_year")
    assert _ranges(months)[1] == (date(2026, 8, 1), date(2026, 10, 31))
    leap = resolve("MTD", _cal(date(2028, 2, 29)), compare="last_year")
    assert _ranges(leap)[1] == (date(2027, 2, 1), date(2027, 2, 28))


def test_a_comparison_before_the_first_sale_is_not_valid():
    anchor = date(2026, 11, 22)
    assert not resolve("L6W", _cal(anchor), compare="last_year").compare_ok  # until late Jul 2027
    assert not resolve("L3M", _cal(anchor)).compare_ok  # May to Jul starts before 23 Jul
    assert resolve("L14D", _cal(anchor)).compare_ok
    nothing = resolve("L14D", _cal(anchor), compare="none")
    assert (nothing.comparison, nothing.compare_ok, nothing.compare_label) == (None, False, None)
    no_sales = Calendar(anchor, None)
    assert not resolve("L14D", no_sales).compare_ok


def test_trading_days_per_window():
    anchor = date(2026, 11, 22)
    trading = {anchor - timedelta(days=d) for d in range(30) if d % 7 != 1}  # closed on Saturdays
    w = resolve("L14D", _cal(anchor, trading))
    assert (w.current.trading_days, w.comparison.trading_days) == (12, 12)
    assert resolve("L1D", _cal(anchor, trading)).current.trading_days == 1


def test_the_trading_days_are_the_golden_days_with_an_order():
    cal = Calendar.from_db(connect(api_tables(), SEEDS, LAST_SALE))
    assert cal.trading_dates == {date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 13)}
    expected = golden.numbers("metrics").set_index("scope")["trading_days"]
    for key in golden.periods(LAST_SALE):
        assert resolve(key, cal).current.trading_days == expected[key], key


def test_calendar_from_the_models():
    cal = Calendar.from_db(connect(api_tables(), SEEDS, LAST_SALE))
    assert (cal.anchor, cal.first_sale) == (LAST_SALE, date(2026, 10, 5))


def _starts(bs: list[Bucket]) -> list[date]:
    return [b.start for b in bs]


def test_buckets_per_grain():
    cal = _cal(date(2026, 10, 31))  # a Saturday
    l6w = resolve("L6W", cal).current
    assert _starts(buckets(l6w)) == [date(2026, 9, 14) + timedelta(weeks=k) for k in range(6)]
    l3m = resolve("L3M", cal).current
    assert buckets(l3m) == [
        Bucket(date(2026, 8, 1), date(2026, 8, 31)),
        Bucket(date(2026, 9, 1), date(2026, 9, 30)),
        Bucket(date(2026, 10, 1), date(2026, 10, 31)),
    ]
    l14d = resolve("L14D", cal).current
    assert len(buckets(l14d)) == 14
    # weeks are calendar weeks: the first and last reach outside the window
    assert _starts(buckets(l14d, "week")) == [date(2026, 10, 12), date(2026, 10, 19),
                                              date(2026, 10, 26)]  # fmt: skip


def test_7_day_blocks_count_back_from_the_window_end():
    l3m = resolve("L3M", _cal(date(2026, 10, 31))).current  # 92 days
    blocks = buckets(l3m, "block_7d")
    assert len(blocks) == 13
    assert blocks[-1] == Bucket(date(2026, 10, 25), date(2026, 10, 31))
    assert blocks[0].start == date(2026, 8, 2)  # 1 Aug is a leftover day without a block
    for d in (l3m.start + timedelta(days=k) for k in range(1, l3m.days)):
        assert bucket_start(d, "block_7d", l3m.end) in _starts(blocks)


def test_the_bucket_before_the_window():
    cal = _cal(date(2026, 10, 31))
    assert bucket_before(resolve("L6W", cal).current) == Bucket(date(2026, 9, 7), date(2026, 9, 13))
    assert bucket_before(resolve("L3M", cal).current) == Bucket(date(2026, 7, 1), date(2026, 7, 31))
    assert not cal.in_history(bucket_before(resolve("L3M", cal).current).start)
    assert bucket_before(resolve("L14D", cal).current).start == date(2026, 10, 17)


def test_bad_names_and_comparisons():
    for name in ["L0D", "L6X", "last_6_weeks", "l6w", "last_n_weeks()"]:
        with pytest.raises(ValueError, match="window"):
            parse_window(name)
    with pytest.raises(ValueError, match="comparison"):
        resolve("L6W", _cal(date(2026, 11, 22)), compare="yoy")


def test_shift_months_keeps_month_ends():
    assert shift_months(date(2029, 2, 28), -12) == date(2028, 2, 29)
    assert shift_months(date(2026, 3, 30), -1) == date(2026, 2, 28)
    assert shift_months(date(2026, 1, 15), 1) == date(2026, 2, 15)
    assert shift_months(date(2026, 3, 31), -12) == date(2025, 3, 31)
