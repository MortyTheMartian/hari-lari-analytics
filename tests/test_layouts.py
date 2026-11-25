"""The grid layout engine (plan 8.4): geometry, bounds, minimum sizes, the page chrome and the
check that no text runs over its slot."""

from __future__ import annotations

import matplotlib
import pytest

from hari_lari_analytics.reporting import layouts
from hari_lari_analytics.reporting.layouts import (
    GridSpec,
    LayoutError,
    SlotSpec,
    check_size,
    check_slots,
    find_overflows,
    place,
    slot_rect,
)
from hari_lari_analytics.reporting.visuals.theme import DEFAULT_THEME


def slot(row=1, col=1, span=12, height=1, component="c"):
    return SlotSpec(component=component, row=row, col=col, span=span, height=height)


# --- geometry -------------------------------------------------------------------------------------


def test_a_full_width_slot_spans_the_usable_width():
    x, _, w, _ = slot_rect(GridSpec(rows=[1]), slot())
    assert x == pytest.approx(layouts.LEFT)
    assert x + w == pytest.approx(layouts.RIGHT)


def test_columns_are_equal_and_separated_by_the_gap():
    grid = GridSpec(rows=[1])
    a = slot_rect(grid, slot(col=1, span=6))
    b = slot_rect(grid, slot(col=7, span=6))
    assert a[2] == pytest.approx(b[2])
    assert b[0] - (a[0] + a[2]) == pytest.approx(layouts.GAP_X)
    thirds = [slot_rect(grid, slot(col=c, span=4))[2] for c in (1, 5, 9)]
    assert thirds == pytest.approx([thirds[0]] * 3)


def test_rows_share_the_usable_height_in_parts():
    grid = GridSpec(rows=[1, 3])
    top = slot_rect(grid, slot(row=1))
    bottom = slot_rect(grid, slot(row=2))
    assert top[1] + top[3] == pytest.approx(layouts.TOP)
    assert bottom[1] == pytest.approx(layouts.BOTTOM)
    assert top[1] - (bottom[1] + bottom[3]) == pytest.approx(layouts.GAP_Y)
    assert bottom[3] == pytest.approx(3 * top[3])


def test_a_tall_slot_covers_its_rows_and_the_gaps_between_them():
    grid = GridSpec(rows=[1, 1, 1])
    one = slot_rect(grid, slot(row=1))
    both = slot_rect(grid, slot(row=1, height=2))
    assert both[3] == pytest.approx(2 * one[3] + layouts.GAP_Y)
    whole = slot_rect(grid, slot(row=1, height=3))
    assert whole[1] == pytest.approx(layouts.BOTTOM)
    assert whole[1] + whole[3] == pytest.approx(layouts.TOP)


def test_a_row_height_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        GridSpec(rows=[1, 0])


# --- bounds and overlap ---------------------------------------------------------------------------


def test_slots_inside_the_grid_are_placed():
    placed = place(GridSpec(rows=[1, 1]), [slot(col=1, span=6), slot(col=7, span=6), slot(row=2)])
    assert [p.slot.row for p in placed] == [1, 1, 2]


def test_a_slot_past_the_last_column_is_refused():
    with pytest.raises(LayoutError, match=r"slot 1 \(c\): col 9 and span 6 run past column 12"):
        check_slots(GridSpec(rows=[1]), [slot(col=9, span=6)], "page p")


def test_a_slot_past_the_last_row_is_refused():
    with pytest.raises(LayoutError, match=r"row 2 and height 2 run past the 2 row\(s\)"):
        check_slots(GridSpec(rows=[1, 1]), [slot(row=2, height=2)])


def test_overlapping_slots_are_refused_and_touching_ones_are_not():
    grid = GridSpec(rows=[1, 1])
    with pytest.raises(LayoutError, match=r"slot 1 \(a\) and slot 2 \(b\) overlap"):
        check_slots(grid, [slot(col=1, span=7, component="a"), slot(col=7, span=6, component="b")])
    check_slots(grid, [slot(col=1, span=6), slot(col=7, span=6), slot(row=2)])


def test_a_tall_slot_overlaps_the_row_under_it():
    grid = GridSpec(rows=[1, 1])
    with pytest.raises(LayoutError, match="overlap"):
        check_slots(grid, [slot(height=2, col=1, span=6), slot(row=2, col=6, span=3)])


def test_every_problem_is_listed_at_once():
    with pytest.raises(LayoutError) as error:
        check_slots(GridSpec(rows=[1]), [slot(col=10, span=5), slot(col=1, span=12, row=2)])
    assert str(error.value).count("\n") == 1


# --- minimum sizes --------------------------------------------------------------------------------


def test_inner_size_takes_off_the_card_padding_and_head():
    rect = (0.1, 0.1, 0.5, 0.4)
    assert layouts.inner_size(rect, False, False) == (0.5, 0.4)
    w, h = layouts.inner_size(rect, True, True)
    assert w == pytest.approx(0.5 - 2 * layouts.CARD_PAD[0])
    assert h == pytest.approx(0.4 - layouts.CARD_HEAD["hint"] - layouts.CARD_PAD[1])
    assert layouts.inner_size(rect, True, False)[1] > h  # the head is shorter without a hint


def test_a_slot_big_enough_for_its_visual_passes():
    check_size(slot_rect(GridSpec(rows=[1]), slot()), "table", True, True)


def test_a_slot_too_small_for_its_visual_is_refused_with_the_numbers():
    rect = slot_rect(GridSpec(rows=[1, 1, 1, 1, 1, 1]), slot(span=2))
    with pytest.raises(LayoutError, match=r"slot x: the visual table needs at least \d+% × \d+%"):
        check_size(rect, "table", True, True, where="slot x")


def test_each_facet_cell_must_reach_the_minimum():
    rect = slot_rect(GridSpec(rows=[1, 1]), slot(span=6))
    check_size(rect, "kpi_card", False, False, (1, 1))
    with pytest.raises(LayoutError, match="in each of 24 cells"):
        check_size(rect, "kpi_card", False, False, (4, 6))


def test_facet_cells_follow_the_layout():
    from hari_lari_analytics.reporting.components import FacetSpec

    row = FacetSpec(by="metric", layout="row")
    assert layouts.facet_cells(None) == (1, 1)
    assert layouts.facet_cells(row, 4) == (1, 4)
    assert layouts.facet_cells(FacetSpec(by="metric", layout="column"), 3) == (3, 1)
    assert layouts.facet_cells(FacetSpec(by="metric", layout="grid(2x3)"), 5) == (2, 3)
    assert layouts.facet_cells(row) == (1, 1)  # an unknown number counts as one


# --- the chrome and the overflow check ------------------------------------------------------------


def chrome_page(**changes):
    fig = layouts.new_page()
    args = dict(
        kicker="Test café",
        title="Overview",
        chips=[("Last 14 days", "fill"), ("30 Sep – 13 Oct 2026", "line")],
        note="The prior period starts before the first sale.",
        footer="Test café · generated 25 Nov 2026",
    )
    layouts.draw_chrome(fig, DEFAULT_THEME, **{**args, **changes})
    return fig


def texts(fig):
    return {t.get_text() for t in layouts.drawn_texts(fig, set())}


def test_the_chrome_draws_header_chips_note_and_footer():
    shown = texts(chrome_page())
    assert {"TEST CAFÉ", "Overview", "Last 14 days", "30 Sep – 13 Oct 2026"} <= shown
    assert "The prior period starts before the first sale." in shown
    assert "Test café · generated 25 Nov 2026" in shown


def test_the_chrome_leaves_out_the_note_and_footer_when_empty():
    shown = texts(chrome_page(note="", footer="", chips=[]))
    assert shown == {"TEST CAFÉ", "Overview"}


def draw_text_in(fig, rect, words, x=None, y=None, size=10):
    """Put a text on the page, the way a visual would, and report the overflows of the slot."""
    before = layouts.texts_of(fig)
    with matplotlib.rc_context(DEFAULT_THEME.rc()):
        fig.text(rect[0] + 0.01 if x is None else x, rect[1] + 0.05 if y is None else y, words,
                 fontsize=size)  # fmt: skip
    drawn = layouts.drawn_texts(fig, before)
    return find_overflows(fig, [("slot 1", rect, drawn)])


def test_a_text_inside_its_slot_is_not_an_overflow():
    rect = slot_rect(GridSpec(rows=[1]), slot())
    assert draw_text_in(layouts.new_page(), rect, "Net sales") == []


def test_a_text_that_runs_past_the_right_edge_is_found():
    rect = slot_rect(GridSpec(rows=[1]), slot(span=3))
    found = draw_text_in(layouts.new_page(), rect, "A very long label " * 6)
    assert len(found) == 1
    assert found[0].where == "slot 1" and "right" in found[0].sides and found[0].by > 0.01
    assert "runs over the right edge" in found[0].describe()


def test_a_text_above_or_below_the_slot_is_found():
    rect = slot_rect(GridSpec(rows=[1]), slot(span=6))
    above = draw_text_in(layouts.new_page(), rect, "Up", y=rect[1] + rect[3] + 0.05)
    below = draw_text_in(layouts.new_page(), rect, "Down", y=rect[1] - 0.06)
    assert above[0].sides == ("top",) and below[0].sides == ("bottom",)


def test_a_text_within_the_tolerance_is_not_an_overflow():
    rect = slot_rect(GridSpec(rows=[1]), slot(span=6))
    fig = layouts.new_page()
    before = layouts.texts_of(fig)
    t = fig.text(rect[0] - layouts.OVERFLOW_TOLERANCE / 3, rect[1] + 0.05, "Edge", fontsize=10)
    assert find_overflows(fig, [("s", rect, layouts.drawn_texts(fig, before))]) == []
    assert t.get_text() == "Edge"


def test_drawn_texts_skip_what_is_old_empty_hidden_or_on_an_axis_that_is_off():
    fig = layouts.new_page()
    fig.text(0.1, 0.1, "old")
    before = layouts.texts_of(fig)
    fig.text(0.1, 0.2, "new")
    fig.text(0.1, 0.3, "   ")
    fig.text(0.1, 0.4, "hidden", visible=False)
    off = fig.add_axes((0.5, 0.5, 0.2, 0.2))
    off.text(0.5, 0.5, "axis off")
    off.set_axis_off()
    on = fig.add_axes((0.5, 0.1, 0.2, 0.2))
    on.text(0.5, 0.5, "axis on")
    assert {t.get_text() for t in layouts.drawn_texts(fig, before)} >= {"new", "axis on"}
    names = {t.get_text() for t in layouts.drawn_texts(fig, before)}
    assert not names & {"old", "   ", "hidden", "axis off"}
