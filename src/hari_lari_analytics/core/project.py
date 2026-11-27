"""The project settings of `project.yml`: currency and number format, time zone, first day of
the week, the default sample-size rule, the anchor rule and the reference date of the loader.

The file is read once per process (`project()`): the one named by `$PROJECT_FILE` or, without
it, the packaged `project.yml`. It is validated strictly, so a typo or a missing key fails with
the file name and the key. Tests that need other settings use `use_project`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV_VAR = "PROJECT_FILE"
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS: tuple[Weekday, ...] = get_args(Weekday)
DASH = "–"  # shown for a missing value


class ProjectError(ValueError):
    """`project.yml` is invalid; the message names the file and the key."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Currency(_Strict):
    code: str = Field(pattern=r"^[A-Z]{3}$")
    decimals: int = Field(default=0, ge=0, le=4)


class CompactTier(_Strict):
    """A short form of large numbers: from `from_` on, value / `unit` with the suffix."""

    from_: float = Field(alias="from", gt=0)
    unit: float = Field(gt=0)
    suffix: str
    decimals: int = Field(default=0, ge=0, le=4)


class NumberFormat(_Strict):
    """How numbers are written. Every method returns `–` for a missing value."""

    thousands_separator: str = Field(default=",", max_length=1)
    decimal_separator: str = Field(default=".", pattern=r"^[.,]$")
    compact: list[CompactTier] = []

    @field_validator("compact")
    @classmethod
    def _largest_first(cls, tiers: list[CompactTier]) -> list[CompactTier]:
        if any(a.from_ <= b.from_ for a, b in zip(tiers, tiers[1:], strict=False)):
            raise ValueError("list the tiers largest first: `from` must fall from one to the next")
        return tiers

    @model_validator(mode="after")
    def _distinct(self) -> NumberFormat:
        if self.thousands_separator == self.decimal_separator:
            raise ValueError("the thousands and the decimal separator must differ")
        return self

    def localize(self, text: str) -> str:
        """Swap the separators of a number written with `,` for thousands and `.` for decimals."""
        if (self.thousands_separator, self.decimal_separator) == (",", "."):
            return text
        return text.translate(
            {ord(","): self.thousands_separator, ord("."): self.decimal_separator}
        )

    def number(self, value: float, decimals: int = 0) -> str:
        return DASH if pd.isna(value) else self.localize(f"{value:,.{decimals}f}")

    def percent(self, value: float, decimals: int = 1, *, signed: bool = False) -> str:
        if pd.isna(value):
            return DASH
        return self.localize(f"{value:+.{decimals}%}" if signed else f"{value:.{decimals}%}")

    def compact_number(self, value: float) -> str:
        """The shortest form: 2.50M, 85k, or the number in full below the smallest tier."""
        if pd.isna(value):
            return DASH
        for tier in self.compact:
            if abs(value) >= tier.from_:
                return self.localize(f"{value / tier.unit:.{tier.decimals}f}") + tier.suffix
        return self.number(value)

    def headline(self, value: float, decimals: int = 0, *, short: bool = False) -> str:
        """A big KPI number: with `short`, a value reaching the largest tier (2.50M) is
        shortened, everything else is written in full."""
        if pd.isna(value):
            return DASH
        if short and self.compact and value >= self.compact[0].from_:
            tier = self.compact[0]
            return self.localize(f"{value / tier.unit:.{tier.decimals}f}") + tier.suffix
        return self.number(value, decimals)


class Project(_Strict):
    currency: Currency
    numbers: NumberFormat
    timezone: str
    first_weekday: Weekday
    min_n: int = Field(ge=1)
    anchor: Literal["yesterday", "today"]
    reference_date: date
    # the Telegram chats a report may deliver to: a name per chat, and the Secret Manager secret
    # that holds its chat id (never the id: this file is public). `admin` is required.
    telegram_targets: dict[str, str]

    @field_validator("telegram_targets")
    @classmethod
    def _targets(cls, targets: dict[str, str]) -> dict[str, str]:
        bad = [n for n in targets if not re.fullmatch(r"[a-z][a-z0-9_]*", n)]
        if bad:
            raise ValueError(f"{', '.join(bad)}: a target name is snake_case")
        secrets = [s for s in targets.values() if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", s)]
        if secrets:
            raise ValueError(f"{', '.join(secrets)}: not a Secret Manager secret name")
        if "admin" not in targets:
            raise ValueError("admin is required: failure alerts and notices go to that chat")
        return targets

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, name: str) -> str:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"{name!r} is not a time zone (use a name such as Asia/Makassar)"
            ) from None
        return name

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def first_isoweekday(self) -> int:
        """1 for monday ... 7 for sunday, as DuckDB's `isodow`."""
        return WEEKDAYS.index(self.first_weekday) + 1

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self, now: datetime | None = None) -> date:
        """The local date; `now` (default: the current time) without a time zone is local."""
        now = now or self.now()
        local = now.astimezone(self.tz) if now.tzinfo else now.replace(tzinfo=self.tz)
        return local.date()

    def default_anchor(self, now: datetime | None = None) -> date:
        """The last day of a report when none is given (`anchor` in project.yml)."""
        return self.today(now) - timedelta(days=1 if self.anchor == "yesterday" else 0)

    def telegram_env(self, target: str) -> str:
        """The environment variable that holds a target's chat id in the job: its secret's name
        in upper case with `_` for `-` (`telegram-chat-id` is `TELEGRAM_CHAT_ID`)."""
        return self.telegram_targets[target].upper().replace("-", "_")

    def week_start(self, day: date) -> date:
        """The first day of the week that holds `day`."""
        return day - timedelta(days=(day.isoweekday() - self.first_isoweekday) % 7)

    def week_end(self, day: date) -> date:
        return self.week_start(day) + timedelta(days=6)


def parse_project(text: str, source: str = "project.yml") -> Project:
    """Validate the text of a project file; `source` names it in the error."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ProjectError(f"{source}: not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ProjectError(f"{source}: expected a mapping of settings")
    try:
        return Project.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in error.errors()
        )
        raise ProjectError(f"{source}: {details}") from None


def load_project(path: str | Path | None = None) -> Project:
    """Load the project file: `path`, else `$PROJECT_FILE`, else the packaged `project.yml`."""
    path = path or os.environ.get(ENV_VAR)
    if path:
        file = Path(path)
        try:
            return parse_project(file.read_text(), str(file))
        except OSError as error:
            raise ProjectError(f"{file}: cannot be read ({error.strerror})") from None
    text = (resources.files("hari_lari_analytics.core") / "project.yml").read_text()
    return parse_project(text)


_current: Project | None = None


def project() -> Project:
    """The settings of this process, loaded on first use."""
    global _current
    if _current is None:
        _current = load_project()
    return _current


@contextmanager
def use_project(settings: Project | None) -> Iterator[Project]:
    """Use other settings inside the block (for tests); `None` reloads the file on first use."""
    global _current
    previous, _current = _current, settings
    try:
        yield project()
    finally:
        _current = previous
