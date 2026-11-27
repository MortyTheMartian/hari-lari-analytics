"""The cases of the golden lines: the fixture run, changed one way each, with the seeds and the
settings it is built with. `lines/<case>.csv` holds what the pandas pipeline made of it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from hari_lari_analytics.core.project import Project, parse_project
from hari_lari_analytics.warehouse import db
from hari_lari_analytics.warehouse.db import Seeds
from hari_lari_analytics.warehouse.inputs import (
    load_aliases,
    load_calendar_events,
    load_category_groups,
)

from ..api_fixture import api_tables

FIXTURES = Path(__file__).parents[1] / "fixtures"
PACKAGED = Path(__file__).parents[2] / "src" / "hari_lari_analytics" / "core" / "project.yml"
LAST_SALE = date(2026, 10, 13)

Tables = dict[str, pd.DataFrame]
Change = Callable[[Tables], None]


def _settings(**changes) -> Project:
    raw = yaml.safe_load(PACKAGED.read_text())
    return parse_project(yaml.safe_dump({**raw, **changes}))


def _aliases(sales_item: str, adjusted_item: str, adjusted_category: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sales_item": [sales_item],
            "adjusted_item": [adjusted_item],
            "adjusted_category": [adjusted_category],
        }
    )


NO_ALIASES = _aliases("", "", "").iloc[0:0]


def _set_variant(variant_id: int, **values) -> Change:
    def change(tables: Tables) -> None:
        variants = tables["item_variants"]
        for column, value in values.items():
            variants.loc[variants["id"] == variant_id, column] = value

    return change


def _both(*changes: Change) -> Change:
    def change(tables: Tables) -> None:
        for c in changes:
            c(tables)

    return change


def _drop_variant_11(tables: Tables) -> None:
    tables["item_variants"] = tables["item_variants"].query("id != 11")


def _no_variants(tables: Tables) -> None:
    del tables["item_variants"]


def _focaccia_replaced(tables: Tables) -> None:
    """Item 3 ("Focaccia Tomato") is deleted, and a live "Focaccia Tomato Special" exists."""
    items = tables["items"]
    items.loc[items["name"] == "Focaccia Tomato", "is_deleted"] = True
    live = pd.DataFrame(
        [(7, "Focaccia Tomato Special", "Snacks (Focaccia)", False)], columns=items.columns
    )
    tables["items"] = pd.concat([items, live], ignore_index=True)


def _second_discount_code(tables: Tables) -> None:
    discounts = tables["transaction_line_discounts"]
    extra = discounts.assign(discount_name="CLEANUP20")
    tables["transaction_line_discounts"] = pd.concat([discounts, extra], ignore_index=True)


@dataclass(frozen=True)
class Case:
    anchor: date = LAST_SALE
    change: Change | None = None
    settings: Project | None = None
    aliases: pd.DataFrame | None = None  # default: tests/fixtures/item_aliases.csv

    def tables(self) -> Tables:
        tables = api_tables()
        if self.change:
            self.change(tables)
        return tables

    def item_aliases(self) -> pd.DataFrame:
        if self.aliases is not None:
            return self.aliases
        return load_aliases(FIXTURES / "item_aliases.csv")

    def seeds(self) -> Seeds:
        return Seeds(
            item_aliases=self.item_aliases(),
            category_groups=load_category_groups(FIXTURES / "category_group.csv"),
            calendar_events=load_calendar_events(FIXTURES / "calendar_events.csv"),
        )


LINE_CASES = {
    "fixture": Case(),
    "anchor_2026-10-06": Case(anchor=date(2026, 10, 6)),
    "first_weekday_sunday": Case(settings=_settings(first_weekday="sunday")),
    "first_weekday_friday": Case(settings=_settings(first_weekday="friday")),
    # Moka renamed Latte's Hot variant (id 10)
    "variant_hot_renamed": Case(change=_set_variant(10, name="large")),
    "variant_hot_without_name": Case(change=_set_variant(10, name="")),
    # Cold (id 11) was dropped, and Hot kept no name
    "variant_cold_deleted": Case(
        change=_both(_set_variant(11, is_deleted=True), _set_variant(10, name=""))
    ),
    "variant_cold_unknown": Case(change=_drop_variant_11),
    "variant_cold_of_another_item": Case(change=_set_variant(11, item_id=2)),
    "no_variants": Case(change=_no_variants),  # a run from before the table was extracted
    "item_renamed_without_alias": Case(aliases=NO_ALIASES),
    "item_alias_wins": Case(aliases=_aliases("Tomato Focaccia", "Focaccia Mushroom", "Snacks")),
    "item_deleted_with_alias": Case(
        change=_focaccia_replaced,
        aliases=_aliases("Tomato Focaccia", "focaccia tomato special", ""),
    ),
    "item_deleted_without_alias": Case(change=_focaccia_replaced, aliases=NO_ALIASES),
    "two_discount_codes": Case(change=_second_discount_code),
}


def connect(case: str):
    """The models of a case."""
    c = LINE_CASES[case]
    return db.connect(c.tables(), c.seeds(), c.anchor, c.settings)
