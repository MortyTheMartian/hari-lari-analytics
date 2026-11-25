"""Components: the YAML schema, the loader, parameters and the overrides of a report slot.

A component is one YAML file in `components/` (docs/components.md, "Components"): `id`,
`title`, `hint`, `description`, default `params`, `query`, `transforms`, `facet`, `visual` and
`insights`. Loading validates it strictly: an unknown key fails with the file and the field,
and the query, the transforms, the facet, the visual and the insight rules are checked against
the registries and the compiler without touching any data (on a fixed calendar, since the
checks need no sales).

A value written `$name` anywhere in the query, the transforms, the facet or the visual is a
parameter, and so is a name used as `{{ name }}` in the title or hint (besides `meta` and
`facet`). The component's `params` give defaults; a report binds the others, or overrides the
defaults. A component whose parameters all have a default is checked completely when loaded; a
component with a parameter only a report binds (`$category`) is checked as far as possible when
loaded and completely when bound. A report slot may change a component with `with:`, a deep
merge (mappings merge, everything else is replaced), which is validated in the same way.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import jinja2
import jinja2.meta
import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
)

from hari_lari_analytics.project import project
from hari_lari_analytics.reporting import insights, transforms, visuals
from hari_lari_analytics.reporting.compiler import Query, QueryError, compile_query
from hari_lari_analytics.reporting.facets import FacetError, FacetSpec, check_facet
from hari_lari_analytics.reporting.queries import SOURCES, QueryContext, SourceQuery, execute
from hari_lari_analytics.reporting.registry import Registry, RegistryError, default_registry
from hari_lari_analytics.reporting.result import Result, Shape
from hari_lari_analytics.reporting.transforms import TransformContext, TransformError
from hari_lari_analytics.reporting.windows import Calendar

PARAMETER = re.compile(r"^\$([a-z_][a-z0-9_]*)$")
NAME = r"^[a-z][a-z0-9_]*$"
TEXT_NAMES = frozenset({"meta", "facet"})  # what a title or hint may use besides the parameters
BODY = ("query", "transforms", "facet", "visual")  # the fields in which $parameters are replaced
TEXT = ("title", "hint")  # Jinja templates
SCHEMA_REF = "# yaml-language-server: $schema=../schema/component.schema.json"


def check_calendar() -> Calendar:
    """The calendar on which the loader compiles every query without data: a long history that
    ends on the `reference_date` of project.yml."""
    return Calendar(anchor=project().reference_date, first_sale=date(2000, 1, 1))


_QUERY_TAGS = ("compiled", "source")
_JINJA = jinja2.Environment()


_STRICT = jinja2.Environment(undefined=jinja2.StrictUndefined)


class ComponentError(ValueError):
    """A component is invalid; the message names the file and the field."""


def render_text(
    template: str | None, params: Mapping[str, Any], meta: Any, facet: Any, strict: bool = True
) -> str:
    """A component's title or hint as the builder shows it. The parameters, the result's `meta`
    and the `facet` are available; an undefined name is an error, or with `strict=False` (a slot
    without data has no `meta`) an empty string."""
    if not template:
        return ""
    env = _STRICT if strict else _JINJA
    return env.from_string(template).render(**params, meta=meta, facet=facet)


# --- the schema -----------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ParamValue = bool | int | float | str | list[bool | int | float | str]


def _query_kind(value: Any) -> str:
    return "source" if isinstance(value, Mapping) and "source" in value else "compiled"


QuerySpec = Annotated[
    Annotated[Query, Tag("compiled")] | Annotated[SourceQuery, Tag("source")],
    Discriminator(_query_kind),
]


def _step(raw: Any) -> Any:
    name, options = transforms.parse_step(raw)
    transforms.parse_options(name, options)
    return raw


StepSpec = Annotated[Any, AfterValidator(_step)]


class VisualSpec(BaseModel):
    """The visual and its options, which the visual registry checks against the query."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ComponentSpec(_Strict):
    id: str = Field(pattern=NAME)
    title: str
    hint: str | None = None
    description: str | None = None
    params: dict[str, ParamValue] = {}
    query: QuerySpec
    transforms: list[StepSpec] = []
    facet: FacetSpec | None = None
    visual: VisualSpec | None = None
    # rules of insights.yml: `movers`, or `{movers: {metrics: [penetration]}}`
    insights: list[str | dict[str, dict[str, Any] | None]] = []

    @field_validator("params")
    @classmethod
    def _names(cls, params: dict) -> dict:
        bad = [p for p in params if not re.match(NAME, p)]
        if bad:
            raise ValueError(f"{', '.join(bad)}: a parameter name is snake_case")
        return params


# --- parameters -----------------------------------------------------------------------------------


def parameters_in(value: Any) -> set[str]:
    """The names of the `$name` values anywhere in a YAML value."""
    if isinstance(value, str):
        m = PARAMETER.match(value)
        return {m[1]} if m else set()
    if isinstance(value, Mapping):
        return set().union(*(parameters_in(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(parameters_in(v) for v in value))
    return set()


def substitute(value: Any, params: Mapping[str, Any]) -> Any:
    """`value` with every bound `$name` replaced by its value (the whole value, of any type)."""
    if isinstance(value, str):
        m = PARAMETER.match(value)
        return params[m[1]] if m and m[1] in params else value
    if isinstance(value, Mapping):
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    return value


def deep_merge(base: Any, override: Any) -> Any:
    """`override` over `base`: mappings merge key by key, any other value is replaced."""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(base[key], value) if key in base else value
        return merged
    return override


def _text_names(template: Any, where: str) -> set[str]:
    if not isinstance(template, str):
        return set()  # the schema reports the wrong type
    try:
        names = jinja2.meta.find_undeclared_variables(_JINJA.parse(template))
    except jinja2.TemplateSyntaxError as error:
        raise ComponentError(f"{where}: {error.message}") from None
    return set(names) - TEXT_NAMES


def _parameters(raw: Mapping, source: str) -> set[str]:
    used = set().union(*(parameters_in(raw.get(f)) for f in BODY))
    for f in TEXT:
        used |= _text_names(raw.get(f), f"{source}: {f}")
    return used


# --- errors ---------------------------------------------------------------------------------------


def _location(loc: Iterable) -> list:
    """A pydantic location without the tags of the query union."""
    parts = list(loc)
    if len(parts) > 1 and parts[0] == "query" and parts[1] in _QUERY_TAGS:
        del parts[1]
    return parts


def _at(raw: Any, loc: list) -> Any:
    for part in loc:
        if isinstance(raw, Mapping) and part in raw:
            raw = raw[part]
        elif isinstance(raw, list) and isinstance(part, int) and part < len(raw):
            raw = raw[part]
        else:
            return None
    return raw


def _message(error: dict) -> str:
    return error["msg"].removeprefix("Value error, ")


def _validation_errors(error: ValidationError, raw: Mapping, unbound: set[str]) -> list[str]:
    """The messages of a validation, without those about a value that holds a parameter not yet
    bound (those are checked when a report binds it)."""
    messages = []
    for e in error.errors():
        loc = _location(e["loc"])
        if unbound and parameters_in(_at(raw, loc)) & unbound:
            continue
        field = ".".join(str(p) for p in loc) or "component"
        messages.append(f"{field}: {_message(e)}")
    return messages


# --- loaded and bound components ------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """A component as loaded: its YAML with the parameters still in it."""

    id: str
    source: str  # the file, for messages
    raw: dict
    defaults: dict[str, Any]
    parameters: frozenset[str]
    registry: Registry

    @property
    def unbound(self) -> frozenset[str]:
        """The parameters a report must bind."""
        return self.parameters - set(self.defaults)

    def bind(
        self, params: Mapping[str, Any] | None = None, with_: Mapping | None = None
    ) -> BoundComponent:
        """The component with its parameters bound (the defaults, then `params`) and the
        overrides of a report slot merged in; checked completely."""
        source = self.source
        raw = self.raw
        if with_:
            if not isinstance(with_, Mapping):
                raise ComponentError(f"{source}: with: expected a mapping of fields")
            if "id" in with_:
                raise ComponentError(f"{source}: with: a slot cannot change the id")
            raw = deep_merge(raw, with_)
            source = f"{source} (with the overrides of the slot)"
        used = _parameters(raw, source)
        values = {**self.defaults, **(params or {})}
        missing = sorted(used - set(values))
        if missing:
            names = ", ".join(f"${p}" for p in missing)
            raise ComponentError(
                f"{source}: the parameter(s) {names} are not bound: give a default in params "
                "or bind them from the report"
            )
        bound = {k: substitute(v, values) if k in BODY else v for k, v in raw.items()}
        try:
            spec = ComponentSpec.model_validate(bound)
        except ValidationError as error:
            raise ComponentError(_join(source, _validation_errors(error, bound, set()))) from None
        query, shape = _check(spec, used, self.registry, source)
        return BoundComponent(self, spec, {k: values[k] for k in sorted(used)}, query, shape)


@dataclass(frozen=True)
class BoundComponent:
    """A component ready to run: every parameter bound, the query with what its transforms
    require added, and the shape of its result after the transforms (None for a Python
    source, whose shape is known only when it runs)."""

    component: Component
    spec: ComponentSpec
    params: dict[str, Any]  # the values of the parameters it uses
    query: Query | SourceQuery
    shape: Shape | None

    @property
    def id(self) -> str:
        return self.spec.id

    def with_query(self, query: Query) -> BoundComponent:
        return dataclasses.replace(self, query=query)

    def run(self, ctx: QueryContext) -> Result:
        result = execute(self.query, ctx)
        steps = self.spec.transforms
        return transforms.run_pipeline(result, steps, TransformContext(ctx.registry, ctx.calendar))


def _join(source: str, messages: list[str]) -> str:
    if len(messages) == 1:
        return f"{source}: {messages[0]}"
    return f"{source}:\n" + "\n".join(f"  - {m}" for m in messages)


def _check(
    spec: ComponentSpec, parameters: set[str], registry: Registry, source: str
) -> tuple[Query | SourceQuery, Shape | None]:
    """Check a bound component against the registries; return its planned query and shape."""
    query = spec.query
    shape: Shape | None = None
    calendar = check_calendar()
    if isinstance(query, SourceQuery):
        if query.source not in SOURCES:
            known = ", ".join(sorted(SOURCES))
            raise ComponentError(f"{source}: query.source: unknown source {query.source} ({known})")
        options = SOURCES[query.source][1]
        try:
            options.model_validate(query.options)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {_message(e)}" for e in error.errors()
            )
            raise ComponentError(f"{source}: query.options: {details}") from None
    else:
        try:
            shape = compile_query(query, registry, calendar).shape
        except (QueryError, RegistryError) as error:
            raise ComponentError(f"{source}: query: {error}") from None
        for i, raw in enumerate(spec.transforms):
            name, _ = transforms.parse_step(raw)
            step = transforms.get(name)
            if shape not in step.shapes:
                raise ComponentError(
                    f"{source}: transforms.{i}: {name} does not work on a result of shape "
                    f"{shape} (it accepts {', '.join(step.shapes)})"
                )
            shape = step.produces or shape
        try:
            query = transforms.plan_query(query, spec.transforms, registry)
            compile_query(query, registry, calendar)
        except TransformError as error:
            raise ComponentError(f"{source}: transforms: {error}") from None
        except (QueryError, RegistryError) as error:
            raise ComponentError(
                f"{source}: query, with what the transforms need: {error}"
            ) from None
    if spec.facet is not None:
        try:
            check_facet(spec.facet, query, parameters, registry, calendar)
        except FacetError as error:
            raise ComponentError(f"{source}: facet: {error}") from None
    if spec.visual is not None:
        try:
            visuals.check_visual(spec.visual.type, spec.visual.options, query, shape, registry)
        except visuals.VisualError as error:
            raise ComponentError(f"{source}: visual: {error}") from None
    try:
        insights.check_insights(spec.insights, query, shape, registry)
    except insights.InsightError as error:
        raise ComponentError(f"{source}: insights: {error}") from None
    unused = sorted(set(spec.params) - parameters)
    if unused:
        raise ComponentError(
            f"{source}: params: {', '.join(unused)} is not used by the component "
            "(write $name in the query, transforms, facet or visual, or {{ name }} in the title)"
        )
    return query, shape


# --- loading --------------------------------------------------------------------------------------


def parse_component(raw: Any, source: str, registry: Registry | None = None) -> Component:
    """A component from its YAML mapping; `source` names it in messages."""
    registry = registry or default_registry()
    if not isinstance(raw, Mapping):
        raise ComponentError(f"{source}: expected a mapping with id, title, query, ...")
    raw = dict(raw)
    defaults = raw.get("params") or {}
    if not isinstance(defaults, Mapping):
        raise ComponentError(f"{source}: params: expected a mapping of parameter defaults")
    used = _parameters(raw, source)
    unbound = used - set(defaults)
    bound = {k: substitute(v, defaults) if k in BODY else v for k, v in raw.items()}
    try:
        ComponentSpec.model_validate(bound)
    except ValidationError as error:
        messages = _validation_errors(error, bound, unbound)
        if messages:
            raise ComponentError(_join(source, messages)) from None
    component = Component(
        id=str(raw.get("id")),
        source=source,
        raw=raw,
        defaults=dict(defaults),
        parameters=frozenset(used),
        registry=registry,
    )
    if not unbound:
        component.bind()  # the complete check
    return component


def load_component(path: Path, registry: Registry | None = None) -> Component:
    """Load one component file; its id must be its file name."""
    source = f"components/{path.name}"
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ComponentError(f"{source}: not valid YAML: {error}") from None
    component = parse_component(raw, source, registry)
    if component.id != path.stem:
        raise ComponentError(f"{source}: id: {component.id} must be the file name ({path.stem})")
    return component


def components_dir() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "components"))


def load_components(
    directory: Path | None = None, registry: Registry | None = None
) -> dict[str, Component]:
    """Every component of a folder (default: the packaged `components/`), by id."""
    directory = directory or components_dir()
    if not directory.is_dir():
        return {}
    return {c.id: c for c in (load_component(p, registry) for p in sorted(directory.glob("*.yml")))}


# --- the JSON Schema ------------------------------------------------------------------------------

_PARAM_SCHEMA = {"type": "string", "pattern": PARAMETER.pattern, "description": "a parameter"}
_WINDOW_PATTERN = r"^(L\d+[DWM]|last_n_(days|weeks|months)\(\d+\)|WTD|MTD|full)$"


def _or_param(schema: dict) -> dict:
    return {"anyOf": [schema, _PARAM_SCHEMA]}


def _enum(values: Iterable[str], description: str | None = None) -> dict:
    schema: dict = {"type": "string", "enum": sorted(values)}
    if description:
        schema["description"] = description
    return schema


def _parametrize(defs: dict, names: Iterable[str]) -> None:
    """Let every property of these definitions (and every list item) also be a `$parameter`."""
    for name in names:
        for prop in defs[name].get("properties", {}).values():
            if prop.get("type") == "array" and "items" in prop:
                prop["items"] = _or_param(prop["items"])
            inner = dict(prop)
            prop.clear()
            prop.update(_or_param(inner))


def component_json_schema(registry: Registry | None = None) -> dict:
    """The JSON Schema of a component file, with the metrics, dimensions, models, transforms and
    their options of the registry, for autocompletion and validation in the editor."""
    registry = registry or default_registry()
    schema = ComponentSpec.model_json_schema()
    defs = schema["$defs"]
    models = sorted(
        m
        for m, spec in registry.models.items()
        if spec.exposed and spec.time and registry.metrics_on(m)
    )

    def metrics() -> dict:
        return _enum(registry.metrics, "a metric of metrics.yml")

    def dimensions() -> dict:
        return _enum(registry.dimensions, "a dimension of dimensions.yml")

    q = defs["Query"]["properties"]
    q["model"] = _enum(models, "the model to query")
    q["metrics"]["items"] = metrics()
    q["dimensions"]["items"] = dimensions()
    q["where"]["propertyNames"] = dimensions()
    q["focus"]["propertyNames"] = dimensions()
    q["window"] = {
        "type": "string",
        "pattern": _WINDOW_PATTERN,
        "description": "L<n>D, L<n>W, L<n>M, last_n_days(n), ..., WTD, MTD or full",
    }
    q["bins"]["propertyNames"] = dimensions()
    defs["TopSpec"]["properties"]["by"] = metrics()
    defs["SourceQuery"]["properties"]["source"] = _enum(SOURCES, "a Python source (@query)")
    defs["TopValues"]["properties"]["by"] = metrics()
    defs["FacetSpec"]["properties"]["by"] = {
        "anyOf": [dimensions(), {"enum": ["period", "metric"]}],
        "description": "a dimension, period or metric",
    }

    steps: list[dict] = [_enum(transforms.TRANSFORMS, "a transform without options")]
    for name, spec in sorted(transforms.TRANSFORMS.items()):
        options = spec.options.model_json_schema()
        for key, value in options.pop("$defs", {}).items():
            if key in defs and defs[key] != value:
                raise ValueError(f"the schema definition {key} is used twice")
            defs[key] = value
        steps.append(
            {
                "type": "object",
                "description": spec.description,
                "properties": {name: {"anyOf": [options, {"type": "null"}]}},
                "required": [name],
                "additionalProperties": False,
            }
        )
    schema["properties"]["transforms"]["items"] = {"anyOf": steps}

    shown: list[dict] = []
    for name, spec in sorted(visuals.VISUALS.items()):
        options = spec.options.model_json_schema()
        for key, value in options.pop("$defs", {}).items():
            if key in defs and defs[key] != value:
                raise ValueError(f"the schema definition {key} is used twice")
            defs[key] = value
        props = {k: _or_param(v) for k, v in options.get("properties", {}).items()}
        shown.append(
            {
                "type": "object",
                "description": spec.description,
                "properties": {"type": {"const": name}, **props},
                "required": ["type", *options.get("required", [])],
                "additionalProperties": False,
            }
        )
    schema["properties"]["visual"] = {
        "anyOf": [{"anyOf": shown}, {"type": "null"}],
        "description": "the visual (`type`) and its options",
    }
    defs.pop("VisualSpec", None)

    rules = insights.default_rules()
    refs: list[dict] = [_enum(rules, "a rule of insights.yml")]
    for name, rule in sorted(rules.items()):
        options = rule.type.options.model_json_schema()
        for key, value in options.pop("$defs", {}).items():
            defs.setdefault(key, value)
        props = {k: _or_param(v) for k, v in options.get("properties", {}).items()}
        props["weight"] = {"type": "number", "exclusiveMinimum": 0}
        overrides = {"type": "object", "properties": props, "additionalProperties": False}
        refs.append(
            {
                "type": "object",
                "description": f"{rule.type.description} (options of the rule {name})",
                "properties": {name: {"anyOf": [overrides, {"type": "null"}]}},
                "required": [name],
                "additionalProperties": False,
            }
        )
    schema["properties"]["insights"] = {
        "type": "array",
        "items": {"anyOf": refs},
        "description": "the insight rules of insights.yml the component uses",
    }

    _parametrize(defs, ["Query", "TopSpec", "SourceQuery", "FacetSpec", "TopValues"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
        "title": "Hari Lari reporting component",
    }


def schema_path() -> Path:
    return Path(str(resources.files("hari_lari_analytics.reporting") / "schema")) / (
        "component.schema.json"
    )


def schema_text(registry: Registry | None = None) -> str:
    return json.dumps(component_json_schema(registry), indent=2) + "\n"


def write_schema(path: Path | None = None) -> Path:
    path = path or schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text())
    return path


if __name__ == "__main__":
    print(write_schema())
