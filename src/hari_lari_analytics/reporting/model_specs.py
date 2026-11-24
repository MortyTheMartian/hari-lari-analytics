"""The model metadata of `models.yml`, beside the SQL of the models in `models/`.

Per model: `description`; `primary_key` (the data checks test it for uniqueness); `time` (the
date column the windows filter on); `calendar` (whether the trading days of `dim_calendar`
apply to its rows); `sample_size` (the count metric of the default sample-size rule, such as
`orders`; none for a model without orders); and `exposed` (whether components may query it;
only `dim_` and `fct_` models can be). Every SQL model has an entry and every entry a model.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

EXPOSABLE_LAYERS = ("dim", "fct")


class ModelError(ValueError):
    """`models.yml` is invalid; the message names the file and the model."""


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    primary_key: list[str] = []
    time: str | None = None
    calendar: bool = False
    sample_size: str | None = None
    exposed: bool = False

    @field_validator("primary_key", mode="before")
    @classmethod
    def _listify(cls, value):
        return [value] if isinstance(value, str) else value


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


def load_models(
    text: str | None = None, names: Iterable[str] | None = None
) -> dict[str, ModelSpec]:
    """Load and validate `models.yml` (default: the packaged file, checked against the SQL
    models). `names`, if given, are the models every entry must match one to one."""
    if text is None:
        text = (resources.files("hari_lari_analytics.reporting") / "models.yml").read_text()
        names = model_sql() if names is None else names
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ModelError("models.yml: expected a mapping of model names")
    specs: dict[str, ModelSpec] = {}
    for model_id, spec in raw.items():
        where = f"models.yml: {model_id}"
        try:
            model = ModelSpec.model_validate({"id": model_id, **(spec or {})})
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}"
                for e in error.errors()
            )
            raise ModelError(f"{where}: {details}") from None
        if model.calendar and model.time is None:
            raise ModelError(f"{where}: calendar needs the time column")
        if model.exposed and model_id.split("_", 1)[0] not in EXPOSABLE_LAYERS:
            raise ModelError(f"{where}: only dim_ and fct_ models can be exposed")
        specs[model_id] = model
    if names is not None:
        names = set(names)
        missing = sorted(names - set(specs))
        if missing:
            raise ModelError(f"models.yml: no entry for the model(s) {', '.join(missing)}")
        unknown = sorted(set(specs) - names)
        if unknown:
            raise ModelError(f"models.yml: {', '.join(unknown)} is not a model in models/")
    return specs


@cache
def default_models() -> dict[str, ModelSpec]:
    return load_models()
