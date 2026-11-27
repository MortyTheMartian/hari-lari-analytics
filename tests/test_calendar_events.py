from datetime import date
from pathlib import Path

import pytest

from hari_lari_analytics.warehouse.inputs import InputError, load_calendar_events

from .conftest import FIXTURES


def test_loads_ranges_and_single_days():
    events = load_calendar_events(FIXTURES / "calendar_events.csv")
    assert len(events) == 4
    demo = events.iloc[0]
    assert (demo["date_from"], demo["date_to"], demo["kind"]) == (
        date(2026, 10, 6),
        date(2026, 10, 6),  # an empty date_to is the same day
        "holiday",  # kinds are lower-cased
    )


def test_missing_file_gives_no_events(tmp_path):
    assert load_calendar_events(tmp_path / "nope.csv").empty


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "calendar_events.csv"
    path.write_text("date_from,date_to,kind,name\n" + body)
    return path


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("06-10-2026,,holiday,Demo\n", "must be a date"),
        ("2026-10-06,2026-10-05,holiday,Demo\n", "before date_from"),
        ("2026-10-06,,festival,Demo\n", "kind must be one of"),
        ("2026-10-06,,holiday,\n", "empty name"),
        ("2026-10-06,,holiday,Demo\n2026-10-06,,holiday,Demo\n", "more than once"),
    ],
)
def test_invalid_rows_are_errors(tmp_path, body, message):
    with pytest.raises(InputError, match=message):
        load_calendar_events(_write(tmp_path, body))


def test_overlapping_events_are_allowed(tmp_path):
    body = "2026-10-06,,holiday,Demo\n2026-10-06,,closure,Closed\n"
    assert len(load_calendar_events(_write(tmp_path, body))) == 2


def test_the_real_calendar_is_valid():
    events = load_calendar_events(Path("config/calendar_events.csv"))
    assert set(events["kind"]) <= {"holiday", "closure"}
    assert events["date_to"].max() == date(2027, 12, 29)
