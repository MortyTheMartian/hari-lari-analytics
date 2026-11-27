"""One in-memory DuckDB connection per run: the run tables, the seeds and the models.

The run tables of one extraction run become `raw_<table>` tables and the config CSV files
`seed_<name>` tables, both with the declared columns cast to fixed types, so an empty table or a
column that is NULL on every row still has the type the models expect. `run_context` holds the
anchor, the first day of the week (from project.yml), which the macro `week_start` reads, and
the time of the extraction.
The models are the SQL files in `models/`, one `create or replace view <name> as ...` each,
named with their layer prefix (`stg_`, `int_`, `dim_`, `fct_`). A model reads only from its own
or a lower layer; within that rule they are built in the order of the names they use, each once
as a table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from graphlib import TopologicalSorter
from pathlib import Path

import duckdb
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from hari_lari_analytics.core.config import DEFAULT as DEFAULT_CONFIG
from hari_lari_analytics.core.config import local_config
from hari_lari_analytics.core.project import Project, project
from hari_lari_analytics.warehouse.checks import CheckReport, check
from hari_lari_analytics.warehouse.inputs import (
    ApiRun,
    InputError,
    load_aliases,
    load_calendar_events,
    load_category_groups,
    load_run,
)
from hari_lari_analytics.warehouse.model_specs import model_sql

LAYER_RANK = {"raw": 0, "seed": 0, "stg": 1, "int": 2, "dim": 3, "fct": 4}


@dataclass(frozen=True)
class Source:
    """The columns the models read from a run table or seed, as DuckDB types."""

    columns: dict[str, str]
    optional: dict[str, str] = field(default_factory=dict)  # NULL when the column is missing
    required: bool = True  # False: a missing or empty table gives an empty view
    source: str = ""  # the source that lands the table (sources.yml); none for a seed


SOURCES_FILE = Path(__file__).with_name("sources.yml")
SOURCE_TYPES = {"varchar", "bigint", "integer", "double", "boolean", "date", "timestamp"}


class _SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: dict[str, str]
    optional: dict[str, str] = {}
    required: bool = True


def load_sources(path: Path = SOURCES_FILE) -> dict[str, Source]:
    """The run tables of `sources.yml`, by table name; an unknown key or column type fails,
    naming the table, and so does a table name used by two sources."""
    raw = yaml.safe_load(path.read_text()) or {}
    sources: dict[str, Source] = {}
    for source, entries in raw.items():
        if not isinstance(entries, dict):
            raise ValueError(f"{path.name}: {source}: expected a mapping of table names")
        for name, entry in entries.items():
            if name in sources:
                raise ValueError(
                    f"{path.name}: {name} is a table of both {sources[name].source} and {source}"
                )
            sources[name] = _source(f"{path.name}: {name}", entry, source)
    return sources


def _source(where: str, entry: object, source: str) -> Source:
    try:
        spec = _SourceSpec.model_validate(entry)
    except ValidationError as error:
        problem = error.errors()[0]
        field_name = ".".join(str(p) for p in problem["loc"])
        raise ValueError(f"{where}: {field_name}: {problem['msg']}") from None
    types = {**spec.columns, **spec.optional}
    unknown = sorted({t for t in types.values() if t not in SOURCE_TYPES})
    if unknown:
        raise ValueError(f"{where}: unknown column type(s) {', '.join(unknown)}")
    return Source(spec.columns, spec.optional, spec.required, source)


RAW_SOURCES = load_sources()


def source_tables(source: str) -> dict[str, Source]:
    """The run tables one source lands."""
    return {name: spec for name, spec in RAW_SOURCES.items() if spec.source == source}


def source_names() -> list[str]:
    return sorted({spec.source for spec in RAW_SOURCES.values()})


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


SEED_FILES = {
    "item_aliases": ("item_aliases.csv", load_aliases),
    "category_groups": ("category_group.csv", load_category_groups),
    "calendar_events": ("calendar_events.csv", load_calendar_events),
}


@dataclass(frozen=True)
class Seeds:
    """The validated config files (see `inputs.py`), as DataFrames."""

    item_aliases: pd.DataFrame
    category_groups: pd.DataFrame
    calendar_events: pd.DataFrame | None = None

    @classmethod
    def from_dir(cls, config_dir: Path) -> Seeds:
        return cls(**{name: load(config_dir / file) for name, (file, load) in SEED_FILES.items()})

    @classmethod
    def load(cls, location: str | Path) -> Seeds:
        """The seeds of a config location (`core/config.py`): a folder or gs://; every file of
        `SEED_FILES` is required."""
        with local_config(location, [file for file, _ in SEED_FILES.values()]) as folder:
            return cls.from_dir(folder)


WEEK_START_MACRO = (
    "create macro week_start(d) as d::date - "
    "((isodow(d) - (select first_weekday from run_context) + 7) % 7)::int"
)


def connect(
    tables: dict[str, pd.DataFrame],
    seeds: Seeds,
    anchor: date | None = None,
    settings: Project | None = None,
    extracted_at: datetime | None = None,
    database: str = ":memory:",
) -> duckdb.DuckDBPyConnection:
    """Register the run tables and seeds and build every model.

    The models end on `anchor` (default: the day of the last payment); later sales are left out.
    The weeks start on the `first_weekday` of `settings` (default: project.yml). `extracted_at`
    is when the run was extracted, the time of the snapshots (stock levels) in local clock time.
    `database` is a DuckDB file to build in (the stored warehouse) instead of memory.
    """
    con = duckdb.connect(database)
    for name, source in RAW_SOURCES.items():
        _register(con, f"raw_{name}", tables.get(name), source)
    for name, source in SEED_SOURCES.items():
        _register(con, f"seed_{name}", getattr(seeds, name), source)
    if anchor is None:
        anchor = con.execute("select max(created_at)::date from raw_transactions").fetchall()[0][0]
    build_on_inputs(con, anchor, settings, extracted_at)
    return con


def build_on_inputs(
    con: duckdb.DuckDBPyConnection,
    anchor: date,
    settings: Project | None = None,
    extracted_at: datetime | None = None,
) -> None:
    """Build the models on the raw_ and seed_ tables already in `con`, cut at `anchor`."""
    settings = settings or project()
    con.create_function("title_case", str.title, ["VARCHAR"], "VARCHAR")  # Python's rules
    con.execute(WEEK_START_MACRO)
    con.execute(
        "create table run_context as select ?::date as anchor, ?::int as first_weekday, "
        "?::timestamp as extracted_at",
        [anchor, settings.first_isoweekday, extracted_at],
    )
    build_models(con)


@dataclass
class RunDb:
    """The models of one run, and the warnings of its data checks."""

    run: ApiRun
    con: duckdb.DuckDBPyConnection
    checks: CheckReport


def open_run(
    location: str | Path, config: str | Path = DEFAULT_CONFIG, anchor: date | None = None
) -> RunDb:
    """Load the latest run in `location` (local or gs://), build the models and check them.

    `config` is the config location (a folder or gs://). A failing data check of severity error
    raises `DataCheckError`.
    """
    run = load_run(location, RAW_SOURCES)
    con = connect(run.tables, Seeds.load(config), anchor, extracted_at=run_time(run))
    return RunDb(run, con, check(con))


def run_time(run: ApiRun) -> datetime | None:
    """When the run was extracted, in local clock time, from its manifest (`run_at` in a
    manifest from before the landing zone)."""
    value = run.manifest.get("started_at") or run.manifest.get("run_at")
    return datetime.fromisoformat(value).replace(tzinfo=None) if value else None


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


def model_select(name: str, sql: str) -> str:
    """The select of a model: what follows its `create or replace view <name> as`."""
    marker = f"create or replace view {name} as"
    if marker not in sql:
        raise ValueError(f"model {name} must define the view {name}")
    return sql.split(marker, 1)[1]


def _tables_read(node: object, tables: set[str], ctes: set[str]) -> None:
    """Collect the base tables and the CTE names in the JSON syntax tree of a select."""
    if isinstance(node, dict):
        if node.get("type") == "BASE_TABLE":
            tables.add(node["table_name"].lower())
        for entry in node.get("cte_map", {}).get("map", []):
            ctes.add(entry["key"].lower())
        for child in node.values():
            _tables_read(child, tables, ctes)
    elif isinstance(node, list):
        for child in node:
            _tables_read(child, tables, ctes)


def model_references(models: dict[str, str]) -> dict[str, set[str]]:
    """The tables and models each model reads, from the syntax tree DuckDB parses.

    DuckDB only parses (`json_serialize_sql`), so nothing has to exist yet and no function has
    to be known; a name in a comment or a string is not a reference, and a CTE is not a table
    (a CTE hides a model of the same name).
    """
    con = duckdb.connect()
    references: dict[str, set[str]] = {}
    for name, sql in models.items():
        select = model_select(name, sql)
        (text,) = con.execute("select json_serialize_sql(?)", [select]).fetchone()
        tree = json.loads(text)
        if tree.get("error"):
            raise ValueError(
                f"model {name}: DuckDB cannot read its SQL: {tree.get('error_message')}"
            )
        tables: set[str] = set()
        ctes: set[str] = set()
        _tables_read(tree, tables, ctes)
        references[name] = tables - ctes - {name}
    return references


def build_order(models: dict[str, str]) -> list[str]:
    """Models in an order that builds every reference first; a reference upwards, or to a run
    table or seed that is not declared, is an error."""
    graph: dict[str, set[str]] = {}
    references = model_references(models)
    sources = {f"raw_{n}" for n in RAW_SOURCES} | {f"seed_{n}" for n in SEED_SOURCES}
    for name in sorted(models):
        layer = LAYER_RANK[name.split("_", 1)[0]]
        for ref in sorted(references[name]):
            prefix = ref.split("_", 1)[0]
            ref_layer = LAYER_RANK.get(prefix)
            if ref_layer is not None and ref_layer > layer:
                raise ValueError(f"model {name} reads {ref} from a higher layer")
            if prefix == "raw" and ref not in sources:
                raise ValueError(
                    f"model {name} reads {ref}, which is not a run table of sources.yml"
                )
            if prefix == "seed" and ref not in sources:
                raise ValueError(f"model {name} reads {ref}, which is not a seed (SEED_SOURCES)")
            if prefix in LAYER_RANK and ref_layer and ref not in models:
                raise ValueError(f"model {name} reads {ref}, which is not a model of models/")
        graph[name] = {r for r in references[name] if r in models}
    return list(TopologicalSorter(graph).static_order())


def build_models(con: duckdb.DuckDBPyConnection) -> None:
    """Build every model once, as a table: the checks and components query them many times."""
    models = model_sql()
    for name in build_order(models):
        view = f"create or replace view {name} as"
        con.execute(models[name].replace(view, f"create or replace table {name} as", 1))
