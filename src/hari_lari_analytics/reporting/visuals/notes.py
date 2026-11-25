"""Text visuals: `notes_card` (a list of rows as short notes) and `insights` (the sentences of
the insight engine). Both have a plain-text form for the Telegram text."""

from __future__ import annotations

from pydantic import Field

from hari_lari_analytics.reporting.compiler import Query
from hari_lari_analytics.reporting.present import Row, present
from hari_lari_analytics.reporting.result import Result
from hari_lari_analytics.reporting.visuals import (
    ALL_SHAPES,
    Area,
    DrawContext,
    Options,
    VisualError,
    visual,
    wrap,
)

LINE = 1.45  # line height, in font sizes


def _draw_items(area: Area, items: list[tuple[str, str, list[str]]], hidden: int = 0) -> None:
    """Items from the top: a mark in its colour, then the wrapped lines of the item. The items
    that do not fit, and `hidden` more, are counted in a last line."""
    theme = area.theme
    step = area.points_y(theme.size("body") * LINE)
    indent = area.points_x(theme.size("body") * 1.3)
    room = max(int(1 / step), 1)
    y, used, drawn = 1.0, 0, 0
    for mark, color, lines in items:
        left = len(items) - drawn + hidden
        if used + len(lines) > room - (1 if left > 1 else 0):
            break
        area.text(0, y, mark, role="body", color=color, va="top")
        for line in lines:
            area.text(indent, y, line, role="body", va="top")
            y -= step
            used += 1
        drawn += 1
    more = len(items) - drawn + hidden
    if more:
        area.text(0, y, f"… and {more} more", role="small", color="muted", va="top")


# --- notes_card ----------------------------------------------------------------------------


class NotesCardOptions(Options):
    column: str  # the column written as the note (a dimension, a metric or a source column)
    before: str | None = None  # a column written in front, such as the day
    after: str | None = None  # a column written behind, such as the amount
    empty: str = "Nothing to note"
    max_items: int = Field(default=5, ge=1)


def _notes_check(options: NotesCardOptions, query, shape, registry) -> None:
    if not isinstance(query, Query):
        return  # a Python source's columns are known only when it runs
    have = [*query.dimensions, *query.metrics]
    named = [c for c in (options.column, options.before, options.after) if c is not None]
    missing = [c for c in named if c not in have]
    if missing:
        raise VisualError(
            f"visual notes_card: {', '.join(missing)} is not a column of the query "
            f"({', '.join(have)})"
        )


def _note(row: Row, options: NotesCardOptions) -> str:
    parts = [row[c].text for c in (options.before, options.column, options.after) if c]
    return " · ".join(p for p in parts if p)


def _note_rows(result: Result, options: NotesCardOptions) -> list[Row]:
    for c in (options.column, options.before, options.after):
        if c is not None and c not in result.df:
            raise VisualError(f"visual notes_card: the result has no column {c}")
    return [r for r in present(result).rows if r.kind != "total"]


def _notes_text(result: Result, options: NotesCardOptions, ctx: DrawContext) -> list[str]:
    rows = _note_rows(result, options)
    if not rows:
        return [options.empty]
    lines = [_note(r, options) for r in rows[: options.max_items]]
    if len(rows) > options.max_items:
        lines.append(f"… and {len(rows) - options.max_items} more")
    return lines


@visual(
    "notes_card",
    NotesCardOptions,
    accepts=("rows", "series"),
    min_size=(0.2, 0.12),
    check=_notes_check,
    text=_notes_text,
)
def notes_card(area: Area, result: Result, options: NotesCardOptions, ctx: DrawContext) -> None:
    """A short list of notes, one per row (the anomalies, the closures): a column with an
    optional column before and after it."""
    rows = _note_rows(result, options)
    if not rows:
        area.text(0, 1, options.empty, color="muted", va="top")
        return
    shown = rows[: options.max_items]
    items = [("–", "muted", wrap(_note(r, options), area, "body", 0.95)) for r in shown]
    _draw_items(area, items, hidden=len(rows) - len(shown))


# --- insights ------------------------------------------------------------------------------


class InsightsOptions(Options):
    max_items: int = Field(default=5, ge=1)
    empty: str = "Nothing stands out"


def _insights_text(result: Result, options: InsightsOptions, ctx: DrawContext) -> list[str]:
    return [i.text for i in list(ctx.insights)[: options.max_items]] or [options.empty]


@visual(
    "insights",
    InsightsOptions,
    accepts=ALL_SHAPES,
    min_size=(0.25, 0.12),
    text=_insights_text,
)
def insights(area: Area, result: Result, options: InsightsOptions, ctx: DrawContext) -> None:
    """The insight sentences of the report or the component, ranked, each with a dot in the
    colour of its tone."""
    theme = area.theme
    shown = list(ctx.insights)[: options.max_items]
    if not shown:
        area.text(0, 1, options.empty, color="muted", va="top")
        return
    items = [("●", theme.tone_color(i.tone), wrap(i.text, area, "body", 0.95)) for i in shown]
    _draw_items(area, items)
