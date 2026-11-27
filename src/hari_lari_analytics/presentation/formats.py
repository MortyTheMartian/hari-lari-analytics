"""Display strings: every number, change, bucket and window a report shows is written here.

The module does not import matplotlib, so the PDF visuals, the Telegram text and the JSON bundle
(plan 8.9) all write the same strings. The separators, the currency and the short forms come
from `project.yml` (`project()`). A missing value is `–`, a comparison that is not valid `n/a`.

The helpers `idr`, `pct`, `compact`, `headline`, `stamp` and `change_text` write the strings
of the report before the component port (plan 9). The others write a value by its
metric's format in a style, and a change, delta or growth by the rules of docs/components.md,
"Visuals": a signed percentage for amounts and counts, percentage points for `pct` metrics.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

import pandas as pd

from hari_lari_analytics.analysis.windows import Grain
from hari_lari_analytics.core.project import DASH, project
from hari_lari_analytics.semantic.registry import Format, Metric

NA = "n/a"  # a comparison that is not valid
CHANGE_CAP = 9.99  # a ratio change above this is written >+999%

Style = Literal["full", "compact", "headline"]
ChangeKind = Literal["change", "delta", "growth"]
Tone = Literal["good", "bad", "neutral"]

# --- the helpers of today's report ------------------------------------------------------------


def idr(value: float) -> str:
    return project().numbers.number(value, project().currency.decimals)


def pct(value: float, digits: int = 1) -> str:
    return project().numbers.percent(value, digits)


def compact(value: float) -> str:
    return project().numbers.compact_number(value)


def headline(value: float, is_idr: bool) -> str:
    """Big KPI number: millions for large amounts, otherwise the full figure."""
    decimals = project().currency.decimals if is_idr else 0
    return project().numbers.headline(value, decimals, short=is_idr)


def date_range(start: date, end: date) -> str:
    """A range of days, short where the month or year repeats: 16–22 Nov 2026."""
    if start.year != end.year:
        return f"{start:%-d %b %Y} – {end:%-d %b %Y}"
    if start.month != end.month:
        return f"{start:%-d %b} – {end:%-d %b %Y}"
    return f"{start.day}–{end:%-d %b %Y}"


def days_ending(end: date, days: int, before: int = 0) -> str:
    """The `days` days ending on `end`, moved back by `before` days, as a date range: the week
    of a caption (`days_ending(anchor, 7)`) and the week before it (`before=7`)."""
    last = end - timedelta(days=before)
    return date_range(last - timedelta(days=days - 1), last)


def stamp(value) -> str:
    return "–" if pd.isna(value) else f"{value:%d %b %H:%M}"


def change_text(value: float, digits: int = 1) -> str:
    if pd.isna(value):
        return "n/a"
    text = ">+999%" if value > CHANGE_CAP else project().numbers.percent(value, digits, signed=True)
    return ("▲ " if value >= 0 else "▼ ") + text


# --- values by format -------------------------------------------------------------------------

_DECIMALS = {"decimal": 2, "pct": 1}


def is_missing(value) -> bool:
    return value is None or (not isinstance(value, str) and bool(pd.isna(value)))


def unit(fmt: Format) -> str:
    """The unit written beside a value of this format: the currency for amounts."""
    return project().currency.code if fmt in ("idr", "idr_compact") else ""


def value_text(value, fmt: Format, style: Style = "full") -> str:
    """A value of a metric with this format, without its unit.

    `full`: every digit (1,234,567; 2.05; 53.2%). `compact`: the short form of large numbers
    (1.23M, 85k). `headline`: the big KPI number, short only for amounts from the largest tier
    on. A timestamp is written as day, month and time in every style."""
    if is_missing(value):
        return DASH
    numbers = project().numbers
    if fmt == "timestamp":
        return stamp(pd.Timestamp(value))
    if fmt == "pct":
        return numbers.percent(value, _DECIMALS["pct"])
    if fmt == "decimal":
        if style == "compact" and abs(value) >= 10_000:
            return numbers.compact_number(value)
        return numbers.number(value, _DECIMALS["decimal"])
    decimals = project().currency.decimals if fmt in ("idr", "idr_compact") else 0
    if fmt == "idr_compact" or style == "compact":
        return numbers.compact_number(value)
    if style == "headline":
        return numbers.headline(value, decimals, short=fmt == "idr")
    return numbers.number(value, decimals)


def signed_number(value, decimals: int = 0) -> str:
    if is_missing(value):
        return DASH
    text = project().numbers.number(abs(value), decimals)
    return ("+" if value > 0 else "-" if value < 0 else "±") + text


def signed_compact(value) -> str:
    if is_missing(value):
        return DASH
    text = project().numbers.compact_number(abs(value))
    return ("+" if value > 0 else "-" if value < 0 else "±") + text


def ratio_text(value) -> str:
    """A ratio change or growth: +12.3%, capped at >+999%."""
    if is_missing(value):
        return DASH
    if value > CHANGE_CAP:
        return ">+999%"
    return project().numbers.percent(value, 1, signed=True)


def points_text(value) -> str:
    """A difference of two shares, given as a fraction: +1.2 pp."""
    if is_missing(value):
        return DASH
    return project().numbers.localize(f"{value * 100:+.1f}") + " pp"


def delta_text(value, fmt: Format, style: Style = "full") -> str:
    """A difference in the unit of the metric: +3,812 (or +3.8k compact), +1.2 pp, +0.12."""
    if fmt == "pct":
        return points_text(value)
    if fmt == "decimal":
        return signed_number(value, _DECIMALS["decimal"])
    if fmt == "idr_compact" or style == "compact":
        return signed_compact(value)
    decimals = project().currency.decimals if fmt == "idr" else 0
    return signed_number(value, decimals)


def default_change(metric: Metric) -> ChangeKind:
    """The change a visual shows by default: percentage points for a share, a ratio otherwise."""
    return "delta" if metric.format == "pct" else "change"


def comparison_text(value, metric: Metric, kind: ChangeKind, style: Style = "full") -> str:
    """The display string of a change, delta or growth of a metric; the growth of a share or
    rate is a difference, in percentage points."""
    if kind == "delta" or (kind == "growth" and metric.format == "pct"):
        return delta_text(value, metric.format, style)
    return ratio_text(value)


def tone(value, up_is_good: bool | None) -> Tone | None:
    """good or bad by the sign and the direction of the metric; neutral for a neutral metric or
    no change; None for a missing value."""
    if is_missing(value):
        return None
    if up_is_good is None or value == 0:
        return "neutral"
    return "good" if (value > 0) == up_is_good else "bad"


# --- buckets, windows and dates ---------------------------------------------------------------

GRAIN_WORD: dict[Grain, str] = {"day": "day", "week": "week", "month": "month", "block_7d": "week"}
GROWTH_WORD: dict[Grain, str] = {"week": "week-over-week", "month": "month-over-month"}


def bucket_tick(start: date, grain: Grain) -> str:
    """The two-line axis label of a bucket: Mon / 05, W45 / 02 Nov, Nov / 2026."""
    if grain == "day":
        return f"{start:%a}\n{start:%d}"
    if grain == "month":
        return f"{start:%b}\n{start:%Y}"
    if grain == "week":
        return f"W{start.isocalendar().week:02d}\n{start:%d %b}"
    return f"{start:%d %b}"


def bucket_label(start: date, end: date, grain: Grain) -> str:
    """A bucket in a sentence: Mon 05 Oct, the week of 02 Nov, November 2026, 16 – 22 Nov."""
    if grain == "day":
        return f"{start:%a %d %b}"
    if grain == "month":
        return f"{start:%B %Y}"
    if grain == "week":
        return f"the week of {start:%d %b}"
    return f"{start:%d %b} – {end:%d %b}"


def day_text(value: date | datetime) -> str:
    return f"{value:%d %b %Y}"
