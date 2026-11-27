"""project.yml (plan 7A.4): the loader, and each setting where the code reads it."""

import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from hari_lari_analytics.analysis import windows
from hari_lari_analytics.core.project import (
    ENV_VAR,
    Project,
    ProjectError,
    load_project,
    parse_project,
    project,
    use_project,
)
from hari_lari_analytics.presentation.components import check_calendar
from hari_lari_analytics.presentation.formats import (
    change_text,
    compact,
    date_range,
    headline,
    idr,
    pct,
)
from hari_lari_analytics.semantic.registry import RegistryError, load_registry
from hari_lari_analytics.sources.moka.checks import last_week_bounds

from .test_models import golden_case

ROOT = Path(__file__).parent.parent
PACKAGED = ROOT / "src" / "hari_lari_analytics" / "core" / "project.yml"


def make(**changes) -> Project:
    """The packaged settings with some top-level keys replaced."""
    raw = yaml.safe_load(PACKAGED.read_text())
    return parse_project(yaml.safe_dump({**raw, **changes}))


SUNDAY = make(first_weekday="sunday")
EUROPE = make(
    currency={"code": "EUR", "decimals": 2},
    numbers={
        "thousands_separator": ".",
        "decimal_separator": ",",
        "compact": [{"from": 1000000, "unit": 1000000, "suffix": " Mio", "decimals": 1}],
    },
)


# ---- the file and the loader ---------------------------------------------------------
def test_the_packaged_file_holds_todays_settings():
    p = project()
    assert (p.currency.code, p.currency.decimals) == ("IDR", 0)
    assert (p.numbers.thousands_separator, p.numbers.decimal_separator) == (",", ".")
    assert p.timezone == "Asia/Makassar" and p.tz == ZoneInfo("Asia/Makassar")
    assert (p.first_weekday, p.first_isoweekday) == ("monday", 1)
    assert (p.min_n, p.anchor, p.reference_date) == (30, "yesterday", date(2026, 11, 22))


def _bad(**changes):
    raw = {**yaml.safe_load(PACKAGED.read_text()), **changes}
    return yaml.safe_dump({k: v for k, v in raw.items() if v is not ...})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_bad(colour="red"), "colour: Extra inputs are not permitted"),
        (_bad(min_n=...), "min_n: Field required"),
        (_bad(min_n=0), "min_n: Input should be greater than or equal to 1"),
        (_bad(timezone="Bali"), "timezone: Value error, 'Bali' is not a time zone"),
        (_bad(first_weekday="Monday"), "first_weekday: Input should be"),
        (_bad(anchor="tomorrow"), "anchor: Input should be"),
        (_bad(reference_date="soon"), "reference_date: Input should be a valid date"),
        (_bad(currency={"code": "idr"}), "currency.code: String should match pattern"),
        (_bad(currency={"code": "IDR", "unit": 1}), "currency.unit: Extra inputs"),
        (
            _bad(numbers={"thousands_separator": ",", "decimal_separator": ","}),
            "must differ",
        ),
        (
            _bad(numbers={"compact": [{"from": 10, "unit": 1, "suffix": "k"},
                                      {"from": 100, "unit": 1, "suffix": "h"}]}),
            "largest first",
        ),
        ("- a\n- b\n", "expected a mapping"),
        ("a: [", "not valid YAML"),
    ],
)  # fmt: skip
def test_a_bad_file_fails_with_its_name_and_the_key(text, message):
    with pytest.raises(ProjectError, match=re.escape(message)) as error:
        parse_project(text, "custom.yml")
    assert str(error.value).startswith("custom.yml: ")


def test_project_file_variable_names_another_file(tmp_path, monkeypatch):
    other = tmp_path / "demo.yml"
    other.write_text(_bad(min_n=12, timezone="Europe/Prague"))
    monkeypatch.setenv(ENV_VAR, str(other))
    loaded = load_project()
    assert (loaded.min_n, loaded.timezone) == (12, "Europe/Prague")
    assert load_project(PACKAGED).min_n == 30  # an explicit path wins
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "missing.yml"))
    with pytest.raises(ProjectError, match="missing.yml: cannot be read"):
        load_project()


def test_use_project_swaps_the_settings_for_a_block():
    before = project()
    with use_project(SUNDAY) as inside:
        assert inside is project() and project().first_weekday == "sunday"
    assert project() is before


def test_the_deploy_files_use_the_project_time_zone():
    zone = project().timezone
    assert re.search(rf"^\s*TZ={zone}\s*$", (ROOT / "Dockerfile").read_text(), re.M)
    assert f"TZ={zone}" in (ROOT / "deploy" / "setup.sh").read_text()
    from hari_lari_analytics.orchestration.pipeline_cli import schedule

    assert schedule()["timezone"] == zone  # the scheduler job of the tick


# ---- number format: the old report's helpers, unchanged for today's settings ----------
def _old_idr(v):
    return "–" if pd.isna(v) else f"{v:,.0f}"


def _old_pct(v, digits=1):
    return "–" if pd.isna(v) else f"{v:.{digits}%}"


def _old_compact(v):
    if pd.isna(v):
        return "–"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:,.0f}"


def _old_headline(v, is_idr):
    if pd.isna(v):
        return "–"
    return f"{v / 1e6:.2f}M" if is_idr and v >= 1e6 else f"{v:,.0f}"


def _old_change_text(v, digits=1):
    if pd.isna(v):
        return "n/a"
    text = ">+999%" if v > 9.99 else f"{v:+.{digits}%}"
    return ("▲ " if v >= 0 else "▼ ") + text


VALUES = [
    math.nan, None, 0, 0.4, 0.5, 1, 999, 1_000, 9_999, 9_999.6, 10_000, 85_432, 999_999,
    999_999.6, 1_000_000, 2_500_000, 107_559_625, -1, -9_999, -10_000, -2_500_000, 0.0123,
    -0.0456, 0.999, 9.99, 9.995, 12.5,
]  # fmt: skip


def test_the_format_helpers_give_todays_output_with_todays_settings():
    for v in VALUES:
        assert idr(v) == _old_idr(v), v
        assert compact(v) == _old_compact(v), v
        for digits in (0, 1, 2):
            assert pct(v, digits) == _old_pct(v, digits), (v, digits)
            assert change_text(v, digits) == _old_change_text(v, digits), (v, digits)
        for is_idr in (True, False):
            assert headline(v, is_idr) == _old_headline(v, is_idr), (v, is_idr)


def test_other_separators_decimals_and_compact_suffixes():
    with use_project(EUROPE):
        assert idr(1234567.891) == "1.234.567,89"
        assert idr(math.nan) == "–"
        assert pct(0.1234) == "12,3%"
        assert change_text(0.051) == "▲ +5,1%" and change_text(-0.2) == "▼ -20,0%"
        assert compact(2_500_000) == "2,5 Mio" and compact(85_000) == "85.000"
        assert headline(2_500_000, True) == "2,5 Mio" and headline(2_500_000, False) == "2.500.000"
        assert headline(1234.5, True) == "1.234,50"
    assert idr(1234567.891) == "1,234,568"  # and back to today's


def test_no_separator_and_no_compact_form():
    plain = make(numbers={"thousands_separator": "", "compact": []})
    assert plain.numbers.number(1234567) == "1234567"
    assert plain.numbers.compact_number(2_500_000) == "2500000"
    assert plain.numbers.headline(2_500_000, short=True) == "2500000"


@pytest.mark.parametrize(
    ("start", "end", "text"),
    [
        (date(2026, 11, 16), date(2026, 11, 22), "16–22 Nov 2026"),
        (date(2026, 11, 30), date(2026, 12, 6), "30 Nov – 6 Dec 2026"),
        (date(2026, 12, 28), date(2027, 1, 3), "28 Dec 2026 – 3 Jan 2027"),
    ],
)
def test_date_range(start, end, text):
    assert date_range(start, end) == text


# ---- time zone and the anchor rule -------------------------------------------------
def test_the_anchor_is_yesterday_in_the_project_time_zone():
    p = project()
    # 20:00 UTC on 23 Nov is already 04:00 on Tuesday 24 Nov in Bali (+08:00)
    utc = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
    assert p.default_anchor(utc) == date(2026, 11, 23)
    assert p.default_anchor(utc - timedelta(hours=5)) == date(2026, 11, 22)
    assert p.default_anchor(datetime(2026, 11, 24, 4, 0)) == date(2026, 11, 23)  # naive: local
    assert p.default_anchor(datetime(2026, 11, 24, 4, 0, tzinfo=p.tz)) == date(2026, 11, 23)


def test_the_anchor_rule_can_be_today():
    assert make(anchor="today").default_anchor(datetime(2026, 11, 24, 4, 0)) == date(2026, 11, 24)


def test_now_is_in_the_project_time_zone():
    assert project().now().utcoffset() == timedelta(hours=8)
    assert make(timezone="Europe/Prague").now().tzinfo == ZoneInfo("Europe/Prague")


# ---- the first day of the week ----------------------------------------------------
def test_week_start_and_end():
    monday, sunday = project(), SUNDAY
    for d in (date(2026, 11, 22), date(2026, 11, 23), date(2026, 11, 28)):  # Sun, Mon, Sat
        assert monday.week_start(d) == d - timedelta(days=d.weekday())
        assert monday.week_end(d) == monday.week_start(d) + timedelta(days=6)
    assert sunday.week_start(date(2026, 11, 22)) == date(2026, 11, 22)  # a Sunday starts it
    assert sunday.week_start(date(2026, 11, 28)) == date(2026, 11, 22)
    assert sunday.week_end(date(2026, 11, 22)) == date(2026, 11, 28)
    assert make(first_weekday="saturday").week_start(date(2026, 11, 22)) == date(2026, 11, 21)


def test_complete_weeks_end_on_the_day_before_the_first_weekday():
    wed = date(2026, 11, 25)
    assert windows.last_n_weeks(wed, 1).end == date(2026, 11, 22)  # Sunday
    assert windows.last_n_weeks(date(2026, 11, 22), 1).start == date(2026, 11, 16)
    with use_project(SUNDAY):
        week = windows.last_n_weeks(wed, 2)
        assert (week.start, week.end) == (date(2026, 11, 8), date(2026, 11, 21))  # Sun to Sat
        assert windows.last_n_weeks(date(2026, 11, 21), 1).end == date(2026, 11, 21)
        wtd = windows.week_to_date(wed)
        assert (wtd.start, wtd.prior_start) == (date(2026, 11, 22), date(2026, 11, 15))


def test_week_buckets_of_the_windows_follow_the_first_weekday():
    assert windows.bucket_start(date(2026, 11, 22), "week") == date(2026, 11, 16)
    with use_project(SUNDAY):
        assert windows.bucket_start(date(2026, 11, 22), "week") == date(2026, 11, 22)
        assert windows.bucket_start(date(2026, 11, 21), "week") == date(2026, 11, 15)


def test_last_week_of_the_extraction_check_follows_the_first_weekday():
    now = datetime(2026, 11, 24, 4, 0)  # a Tuesday
    assert last_week_bounds(now) == (datetime(2026, 11, 16), datetime(2026, 11, 23))
    with use_project(SUNDAY):
        assert last_week_bounds(now) == (datetime(2026, 11, 15), datetime(2026, 11, 22))


@pytest.mark.parametrize(
    "case, first_weekday",
    [("fixture", None), ("first_weekday_sunday", "sunday"), ("first_weekday_friday", "friday")],
)
def test_the_models_have_the_golden_weeks(case, first_weekday):
    settings = make(first_weekday=first_weekday) if first_weekday else None
    con = golden_case(case)  # all columns, sale_week included
    first = (settings or project()).first_isoweekday
    for model, column in [("fct_sales_lines", "sale_week"), ("fct_orders", "sale_week"),
                          ("dim_calendar", "week_start")]:  # fmt: skip
        days = con.execute(f"select distinct isodow({column}) from {model}").fetchall()
        assert days == [(first,)], model
    rows = con.execute("select date, week_start from dim_calendar").fetchall()
    assert all(0 <= (d - w).days <= 6 for d, w in rows)


# ---- the default sample-size rule ------------------------------------------------
def test_the_default_min_n_comes_from_the_project():
    assert load_registry().default_min_n == project().min_n == 30
    registry = load_registry(settings=make(min_n=45))
    assert registry.min_n("net_sales", "fct_sales_lines") == {"orders": 45}
    assert registry.min_n("net_sales", "fct_orders") == {"orders": 45}
    assert registry.min_n("food_attach_rate", "fct_orders") == {"drink_orders": 30}  # its own


def test_min_n_defaults_is_no_longer_a_metrics_file_entry():
    text = (ROOT / "src/hari_lari_analytics/semantic/metrics.yml").read_text()
    assert not re.search(r"^defaults:", text, re.M)  # moved to project.yml
    with pytest.raises(RegistryError, match=r"metrics.yml: defaults is gone.*project.yml"):
        load_registry("defaults:\n  min_n: 30\n")


# ---- the reference date of the loader ----------------------------------------------
def test_components_are_checked_on_the_reference_date():
    assert check_calendar().anchor == date(2026, 11, 22)
    with use_project(make(reference_date=date(2027, 3, 1))):
        assert check_calendar().anchor == date(2027, 3, 1)


# ---- the Telegram targets of the reports (plan 8.5) --------------------------------
def test_the_telegram_targets_name_secrets_not_chat_ids():
    p = project()
    assert p.telegram_targets == {
        "group": "telegram-chat-id",
        "admin": "telegram-admin-chat-id",
    }
    assert p.telegram_env("group") == "TELEGRAM_CHAT_ID"
    assert p.telegram_env("admin") == "TELEGRAM_ADMIN_CHAT_ID"


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ({"group": "telegram-chat-id"}, "admin is required"),
        ({"Group": "a", "admin": "b"}, "Group: a target name is snake_case"),
        ({"admin": "-1001234"}, "-1001234: not a Secret Manager secret name"),
    ],
)
def test_bad_telegram_targets_are_refused(targets, message):
    with pytest.raises(ProjectError, match=message):
        make(telegram_targets=targets)
