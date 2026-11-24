"""One in-memory DuckDB connection per run: the run tables, the seeds and the models.

The run tables of one extraction run become `raw_<table>` tables and the config CSV files
`seed_<name>` tables, both with the declared columns cast to fixed types, so an empty table or a
column that is NULL on every row still has the type the models expect. `run_context` holds the
anchor. The models are the SQL files in `models/`, one `create or replace view <name> as ...`
each, named with their layer prefix (`stg_`, `int_`, `dim_`, `fct_`). A model reads only from
its own or a lower layer; within that rule they are built in the order of the names they use,
each once as a table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from graphlib import TopologicalSorter
from importlib import resources
from pathlib import Path

import duckdb
import pandas as pd

from hari_lari_analytics.load import (
    InputError,
    load_aliases,
    load_calendar_events,
    load_category_groups,
)
from hari_lari_analytics.load_api import ApiRun, load_run
from hari_lari_analytics.reporting.checks import CheckReport, check

LAYER_RANK = {"raw": 0, "seed": 0, "stg": 1, "int": 2, "dim": 3, "fct": 4}
REFERENCE = re.compile(r"\b((?:raw|seed|stg|int|dim|fct)_\w+)\b")


@dataclass(frozen=True)
class Source:
    """The columns the models read from a run table or seed, as DuckDB types."""

    columns: dict[str, str]
    optional: dict[str, str] = field(default_factory=dict)  # NULL when the column is missing
    required: bool = True  # False: a missing or empty table gives an empty view


RAW_SOURCES = {
    "transactions": Source(
        {
            "id": "varchar",
            "payment_no": "varchar",
            "created_at": "timestamp",
            "parent_payment_id": "varchar",
            "payment_type_label": "varchar",
        },
        optional={"refund_amount": "double"},
    ),
    "transaction_lines": Source(
        {
            "payment_id": "varchar",
            "payment_no": "varchar",
            "uuid": "varchar",
            "item_id": "bigint",
            "item_name": "varchar",
            "item_variant_name": "varchar",
            "item_variant_id": "bigint",
            "category_id": "bigint",
            "category_name": "varchar",
            "quantity": "bigint",
            "gross_sales": "double",
            "discount_amount": "double",
            "net_sales": "double",
        },
        optional={"id": "bigint", "note": "varchar"},
    ),
    "items": Source(
        {"id": "bigint", "name": "varchar", "category.name": "varchar", "is_deleted": "boolean"}
    ),
    "transaction_line_discounts": Source(
        {"payment_id": "varchar", "line_uuid": "varchar", "discount_name": "varchar"},
        required=False,
    ),
    "transaction_line_modifiers": Source(
        {
            "payment_id": "varchar",
            "line_uuid": "varchar",
            "modifier_id": "bigint",
            "modifier_option_id": "bigint",
            "modifier_name": "varchar",
            "modifier_option_name": "varchar",
            "price": "bigint",
        },
        required=False,
    ),
    "transaction_refunds": Source(
        {"payment_id": "varchar", "refunded_reason": "varchar"}, required=False
    ),
}

SEED_SOURCES = {
    "item_aliases": Source(
        {"sales_item": "varchar", "adjusted_item": "varchar", "adjusted_category": "varchar"},
        required=False,
    ),
    "category_groups": Source(
        {
            "category": "varchar",
            "category_group_level_1": "varchar",
            "category_group_level_2": "varchar",
        },
        optional={"category_id": "varchar"},
        required=False,
    ),
    "calendar_events": Source(
        {"date_from": "date", "date_to": "date", "kind": "varchar", "name": "varchar"},
        required=False,
    ),
}


@dataclass(frozen=True)
class Seeds:
    """The validated config files (see `load.py`), as DataFrames."""

    item_aliases: pd.DataFrame
    category_groups: pd.DataFrame
    calendar_events: pd.DataFrame | None = None

    @classmethod
    def from_dir(cls, config_dir: Path) -> Seeds:
        return cls(
            item_aliases=load_aliases(config_dir / "item_aliases.csv"),
            category_groups=load_category_groups(config_dir / "category_group.csv"),
            calendar_events=load_calendar_events(config_dir / "calendar_events.csv"),
        )


def connect(
    tables: dict[str, pd.DataFrame], seeds: Seeds, anchor: date | None = None
) -> duckdb.DuckDBPyConnection:
    """Register the run tables and seeds and build every model.

    The models end on `anchor` (default: the day of the last payment); later sales are left out.
    """
    con = duckdb.connect()
    con.create_function("title_case", str.title, ["VARCHAR"], "VARCHAR")  # Python's rules
    for name, source in RAW_SOURCES.items():
        _register(con, f"raw_{name}", tables.get(name), source)
    for name, source in SEED_SOURCES.items():
        _register(con, f"seed_{name}", getattr(seeds, name), source)
    if anchor is None:
        anchor = con.execute("select max(created_at)::date from raw_transactions").fetchone()[0]
    con.execute("create table run_context as select ?::date as anchor", [anchor])
    build_models(con)
    return con


@dataclass
class RunDb:
    """The models of one run, and the warnings of its data checks."""

    run: ApiRun
    con: duckdb.DuckDBPyConnection
    checks: CheckReport


def open_run(
    location: str | Path, config_dir: Path = Path("config"), anchor: date | None = None
) -> RunDb:
    """Load the latest run in `location` (local or gs://), build the models and check them.

    A failing data check of severity error raises `DataCheckError`.
    """
    run = load_run(location)
    con = connect(run.tables, Seeds.from_dir(config_dir), anchor)
    return RunDb(run, con, check(con))


def _register(
    con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame | None, source: Source
) -> None:
    if df is not None and len(df.columns) == 0:
        df = None  # an empty table can come back from Parquet without any columns
    if df is None:
        if source.required:
            raise InputError(f"The run has no table {name.removeprefix('raw_')}")
        nulls = [f'null::{t} as "{c}"' for c, t in {**source.columns, **source.optional}.items()]
        select = ", ".join([*nulls, "0::bigint as _row"])
        con.execute(f"create table {name} as select {select} where false")
        return
    missing = [c for c in source.columns if c not in df.columns]
    if missing:
        raise InputError(f"{name}: missing column(s) {', '.join(missing)}")
    con.register(f"_df_{name}", df.assign(_row=range(len(df))))
    select = [
        f'cast("{c}" as {t}) as "{c}"' if c in df.columns else f'null::{t} as "{c}"'
        for c, t in {**source.columns, **source.optional}.items()
    ]
    # a table, not a view: every model query would otherwise scan the DataFrame again
    con.execute(f"create table {name} as select {', '.join(select)}, _row from _df_{name}")
    con.unregister(f"_df_{name}")


def model_sql() -> dict[str, str]:
    """The SQL of every model, by name."""
    folder = resources.files("hari_lari_analytics.reporting") / "models"
    models = {}
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            name = entry.name.removesuffix(".sql")
            sql = entry.read_text()
            if f"create or replace view {name} as" not in sql:
                raise ValueError(f"models/{entry.name} must define the view {name}")
            models[name] = sql
    return models


def build_order(models: dict[str, str]) -> list[str]:
    """Models in an order that builds every reference first; a reference upwards is an error."""
    graph: dict[str, set[str]] = {}
    for name, sql in sorted(models.items()):
        layer = LAYER_RANK[name.split("_", 1)[0]]
        refs = {r for r in REFERENCE.findall(sql) if r != name}
        for ref in sorted(refs):
            if LAYER_RANK[ref.split("_", 1)[0]] > layer:
                raise ValueError(f"model {name} reads {ref} from a higher layer")
        graph[name] = {r for r in refs if r in models}
    sorter = TopologicalSorter(graph)
    return list(sorter.static_order())


def build_models(con: duckdb.DuckDBPyConnection) -> None:
    """Build every model once, as a table: the checks and components query them many times."""
    models = model_sql()
    for name in build_order(models):
        view = f"create or replace view {name} as"
        con.execute(models[name].replace(view, f"create or replace table {name} as", 1))
