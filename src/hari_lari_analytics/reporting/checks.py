"""Data checks in the style of dbt tests, defined in `checks.yml` and run on every build.

Per model: `unique`, `not_null`, `accepted_values`, `relationships` and `expressions` (a SQL
condition every row must meet), and a `unique` check of the primary key of `models.yml`; plus
`totals`, conditions over whole models in which
`sum(model.column)` (or min, max, count) stands for that aggregate. A check is an error unless
it says `severity: warn`. Errors stop the build with `DataCheckError`; warnings are returned,
for the data notes of a report.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hari_lari_analytics.load import InputError
from hari_lari_analytics.reporting.model_specs import ModelSpec, default_models

Severity = Literal["error", "warn"]
SAMPLE = 5
AGGREGATE = re.compile(r"\b(sum|min|max|count)\((\w+)\.(\w+)\)")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Options(_Strict):
    severity: Severity = "error"
    message: str | None = None


class Unique(_Options):
    columns: list[str]

    @field_validator("columns", mode="before")
    @classmethod
    def _listify(cls, v):
        return [v] if isinstance(v, str) else v


class NotNull(_Options):
    column: str


class AcceptedValues(_Options):
    values: list[str | int | bool]


class Relationship(_Options):
    to: str = Field(pattern=r"^\w+\.\w+$")


class Expression(_Options):
    expr: str


class ModelChecks(_Strict):
    sample_columns: list[str] = []
    unique: list[Unique] = []
    not_null: list[NotNull] = []
    accepted_values: dict[str, AcceptedValues] = {}
    relationships: dict[str, Relationship] = {}
    expressions: list[Expression] = []

    @field_validator("unique", mode="before")
    @classmethod
    def _unique(cls, items):
        return [i if isinstance(i, dict) else {"columns": i} for i in items]

    @field_validator("not_null", mode="before")
    @classmethod
    def _not_null(cls, items):
        return [i if isinstance(i, dict) else {"column": i} for i in items]

    @field_validator("accepted_values", mode="before")
    @classmethod
    def _accepted(cls, items):
        return {k: v if isinstance(v, dict) else {"values": v} for k, v in items.items()}

    @field_validator("relationships", mode="before")
    @classmethod
    def _relationships(cls, items):
        return {k: v if isinstance(v, dict) else {"to": v} for k, v in items.items()}

    @field_validator("expressions", mode="before")
    @classmethod
    def _expressions(cls, items):
        return [i if isinstance(i, dict) else {"expr": i} for i in items]


class CheckFile(_Strict):
    models: dict[str, ModelChecks] = {}
    totals: list[Expression] = []

    @field_validator("totals", mode="before")
    @classmethod
    def _totals(cls, items):
        return [i if isinstance(i, dict) else {"expr": i} for i in items]


@dataclass(frozen=True)
class Check:
    """One compiled check: `sql` returns the failing rows (for totals: one row if it fails)."""

    name: str
    severity: Severity
    sql: str
    message: str | None = None


@dataclass(frozen=True)
class CheckResult:
    check: Check
    failures: int
    sample: list[tuple]

    @property
    def passed(self) -> bool:
        return self.failures == 0

    def describe(self) -> str:
        what = self.check.message or self.check.name
        text = f"{what}: {self.failures} failing row(s)"
        if self.sample:
            text += f", e.g. {', '.join(_short(row) for row in self.sample)}"
        return text


@dataclass(frozen=True)
class CheckReport:
    results: list[CheckResult]

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and r.check.severity == "error"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and r.check.severity == "warn"]


class DataCheckError(InputError):
    """At least one data check with severity error failed."""

    def __init__(self, report: CheckReport) -> None:
        self.report = report
        lines = "\n".join(f"- {r.describe()}" for r in report.errors)
        super().__init__(f"{len(report.errors)} data check(s) failed:\n{lines}")


def _short(row: tuple) -> str:
    values = [str(v) for v in row]
    return values[0] if len(values) == 1 else "(" + ", ".join(values) + ")"


def load_checks(text: str | None = None) -> CheckFile:
    if text is None:
        text = (resources.files("hari_lari_analytics.reporting") / "checks.yml").read_text()
    return CheckFile.model_validate(yaml.safe_load(text) or {})


def _literal(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def compile_checks(checks: CheckFile, models: Mapping[str, ModelSpec] | None = None) -> list[Check]:
    """The checks of `checks`, plus a unique check of each primary key in `models`."""
    compiled: list[Check] = []
    specs = dict(checks.models)
    for model_id, model in (models or {}).items():
        if model.primary_key:
            spec = specs.get(model_id, ModelChecks())
            if any(u.columns == model.primary_key for u in spec.unique):
                raise ValueError(
                    f"checks.yml: {model_id}: the primary key is unique by models.yml already"
                )
            pk = Unique(columns=model.primary_key)
            specs[model_id] = spec.model_copy(update={"unique": [pk, *spec.unique]})
    for model, spec in specs.items():
        sample = spec.sample_columns

        def cols(*columns: str, sample=sample) -> str:
            return ", ".join(dict.fromkeys([*columns, *sample]))

        for u in spec.unique:
            key = ", ".join(u.columns)
            compiled.append(Check(
                f"{model}: {key} is unique", u.severity,
                f"select {key}, count(*) as n from {model} group by {key} having count(*) > 1",
                u.message,
            ))  # fmt: skip
        for n in spec.not_null:
            compiled.append(Check(
                f"{model}: {n.column} is not null", n.severity,
                f"select {cols(n.column)} from {model} where {n.column} is null", n.message,
            ))  # fmt: skip
        for column, a in spec.accepted_values.items():
            values = ", ".join(_literal(v) for v in a.values)
            compiled.append(Check(
                f"{model}: {column} in ({values})", a.severity,
                f"select distinct {column} from {model} "
                f"where {column} is not null and {column} not in ({values})",
                a.message,
            ))  # fmt: skip
        for column, r in spec.relationships.items():
            target, target_column = r.to.split(".")
            compiled.append(Check(
                f"{model}: every {column} is in {r.to}", r.severity,
                f"select distinct {column} from {model} where {column} is not null and "
                f"{column} not in (select {target_column} from {target} "
                f"where {target_column} is not null)",
                r.message,
            ))  # fmt: skip
        for e in spec.expressions:
            shown = cols() if sample else "*"
            compiled.append(Check(
                f"{model}: {e.expr}", e.severity,
                f"select {shown} from {model} where not coalesce(({e.expr}), false)", e.message,
            ))  # fmt: skip
    for t in checks.totals:
        expr = AGGREGATE.sub(lambda m: f"(select {m[1]}({m[3]}) from {m[2]})", t.expr)
        # select every aggregate too, so a failing total shows both sides
        shown = [f"(select {m[1]}({m[3]}) from {m[2]})" for m in AGGREGATE.finditer(t.expr)]
        compiled.append(Check(
            f"totals: {t.expr}", t.severity,
            f"select {', '.join(shown) or 'null'} where not coalesce(({expr}), false)",
            t.message,
        ))  # fmt: skip
    return compiled


def run_checks(
    con: duckdb.DuckDBPyConnection,
    checks: CheckFile | None = None,
    models: Mapping[str, ModelSpec] | None = None,
) -> CheckReport:
    """Run `checks` (default: `checks.yml` with the primary keys of `models.yml`)."""
    if checks is None:
        checks = load_checks()
        models = default_models() if models is None else models
    results = []
    for check in compile_checks(checks, models):
        try:
            failures = con.execute(f"select count(*) from ({check.sql})").fetchone()[0]
            sample = con.execute(f"{check.sql} limit {SAMPLE}").fetchall() if failures else []
        except duckdb.Error as error:
            raise ValueError(f"check '{check.name}' is not valid SQL: {error}") from error
        results.append(CheckResult(check, failures, sample))
    return CheckReport(results)


def check(
    con: duckdb.DuckDBPyConnection,
    checks: CheckFile | None = None,
    models: Mapping[str, ModelSpec] | None = None,
) -> CheckReport:
    """Run the checks; raise `DataCheckError` if an error-level check fails."""
    report = run_checks(con, checks, models)
    if report.errors:
        raise DataCheckError(report)
    return report
