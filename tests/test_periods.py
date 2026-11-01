from datetime import date

from hari_lari_analytics.periods import last_3_months, last_6_weeks, last_14_days


def test_last_14_days_ends_on_anchor():
    p = last_14_days(date(2026, 10, 31))
    assert (p.start, p.end) == (date(2026, 10, 18), date(2026, 10, 31))
    assert (p.prior_start, p.prior_end) == (date(2026, 10, 4), date(2026, 10, 17))


def test_last_6_weeks_skips_the_incomplete_week():
    p = last_6_weeks(date(2026, 10, 31))  # Saturday
    assert (p.start, p.end) == (date(2026, 9, 14), date(2026, 10, 25))
    assert (p.prior_start, p.prior_end) == (date(2026, 8, 3), date(2026, 9, 13))


def test_last_6_weeks_includes_the_week_ending_on_a_sunday_anchor():
    assert last_6_weeks(date(2026, 10, 25)).end == date(2026, 10, 25)


def test_last_3_months_includes_month_ending_on_anchor():
    p = last_3_months(date(2026, 10, 31))
    assert (p.start, p.end) == (date(2026, 8, 1), date(2026, 10, 31))
    assert (p.prior_start, p.prior_end) == (date(2026, 5, 1), date(2026, 7, 31))


def test_last_3_months_skips_the_incomplete_month_across_year_end():
    p = last_3_months(date(2027, 1, 15))
    assert (p.start, p.end) == (date(2026, 10, 1), date(2026, 12, 31))
    assert (p.prior_start, p.prior_end) == (date(2026, 7, 1), date(2026, 9, 30))
