"""Sanity checks on an extraction. Errors fail the run; warnings only go in the manifest."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

STALE_WARN_DAYS = 3


def last_week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Monday 00:00 to next Monday 00:00 of the week before the one containing `now`."""
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    return monday - timedelta(days=7), monday


def run_checks(
    transactions: pd.DataFrame, lines: pd.DataFrame, now: datetime
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). `now` is naive Bali local time."""
    errors: list[str] = []
    warnings: list[str] = []
    now = now.replace(tzinfo=None)
    if transactions.empty:
        return ["zero payments extracted"], warnings

    created = transactions["created_at"]
    start, end = last_week_bounds(now)
    if not ((created >= start) & (created < end)).any():
        errors.append(f"no receipts in last week ({start:%F} to {end - timedelta(days=1):%F})")

    last = created.max()
    age = now - last.to_pydatetime()
    if age > timedelta(days=STALE_WARN_DAYS):
        warnings.append(f"last sale is {age.days} days old ({last:%F %T})")

    if not lines.empty and "payment_id" in lines and "id" in transactions:
        orphans = (~lines["payment_id"].isin(transactions["id"])).sum()
        if orphans:
            errors.append(f"{orphans} lines do not match a payment")
    return errors, warnings
