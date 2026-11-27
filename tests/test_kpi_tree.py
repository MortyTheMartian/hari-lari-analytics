"""Every identity of kpi_tree.yml holds on real data, to rounding (plan 7.7)."""

import math
from datetime import date
from pathlib import Path

import pytest

from hari_lari_analytics.analysis.windows import Calendar, resolve
from hari_lari_analytics.semantic.registry import (
    Identity,
    Registry,
    default_registry,
    identity_models,
    load_registry,
)
from hari_lari_analytics.warehouse.db import connect, open_run

from .api_fixture import api_tables
from .semantic import evaluate
from .test_models import LAST_SALE, REAL_RUN, SEEDS

REG = default_registry()
WINDOWS = ["full", "L14D", "L6W", "L3M"]


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _missing(*values) -> bool:
    return any(v is None or (isinstance(v, float) and math.isnan(v)) for v in values)


def _holds(identity: Identity, row) -> bool | None:
    """Whether a product or sum identity holds on one row; None where it is undefined."""
    values = [row[m] for _, m in identity.terms]
    if _missing(row[identity.kpi], *values):
        return None
    if identity.kind == "product":
        return _close(row[identity.kpi], math.prod(values))
    return _close(row[identity.kpi], sum(s * row[m] for s, m in identity.terms))


def check_identity(con, registry: Registry, identity: Identity, model: str, window=None):
    """(checks done, failures) of one identity on one model, over one window or everything."""
    span = {} if window is None else {"start": window.start, "end": window.end}
    checked, failures = 0, []
    if identity.kind != "sum_over":
        row = evaluate(con, registry, model, identity.metrics, **span).iloc[0]
        ok = _holds(identity, row)
        if ok is not None:
            checked += 1
            if not ok:
                failures.append(f"{identity.id} on {model}: {row[identity.metrics].to_dict()}")
        return checked, failures

    then = registry.identity(identity.then) if identity.then else None
    metrics = [*identity.metrics, *(then.metrics if then else [])]
    rows = evaluate(con, registry, model, metrics, by=identity.dimension, **span)
    total = rows[rows["is_total"]].iloc[0]
    members = rows[~rows["is_total"]]
    if not _missing(total[identity.kpi]):
        checked += 1
        parts = members[identity.driver].sum()
        if not _close(total[identity.kpi], parts):
            failures.append(f"{identity.id} on {model}: {total[identity.kpi]} != Σ {parts}")
    for _, member in members.iterrows() if then else ():
        ok = _holds(then, member)
        if ok is not None:
            checked += 1
            if not ok:
                failures.append(f"{then.id} for {member[identity.dimension]} on {model}")
    return checked, failures


def check_tree(con, registry: Registry) -> dict[str, int]:
    """Check every identity on every model and window; return the checks done per identity."""
    calendar = Calendar.from_db(con)
    done: dict[str, int] = {}
    failures: list[str] = []
    for identity in registry.identities.values():
        done[identity.id] = 0
        for model in sorted(identity_models(registry, identity)):
            for name in WINDOWS:
                window = None if name == "full" else resolve(name, calendar).current
                n, failed = check_identity(con, registry, identity, model, window)
                done[identity.id] += n
                failures += [f"{name}: {f}" for f in failed]
    assert not failures, "\n".join(failures)
    return done


def test_every_identity_holds_on_the_fixture():
    done = check_tree(connect(api_tables(), SEEDS, LAST_SALE), REG)
    assert all(done.values()), done


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_every_identity_holds_on_the_real_run():
    db = open_run(REAL_RUN, Path("config"), date(2026, 11, 22))
    done = check_tree(db.con, REG)
    # every identity was checked on both models where it exists, in all four windows
    assert done["sales_volume_value"] == 2 * len(WINDOWS)
    assert done["aov_by_category"] > 10 * len(WINDOWS)


def test_a_wrong_identity_fails():
    registry = load_registry(
        kpi_tree="""
        - {id: wrong, kpi: net_sales, kind: product, drivers: [orders, items_per_order]}
        - {id: wrong_sum, kpi: net_sales, kind: sum, drivers: [gross_sales, -discounts]}
        """
    )
    con = connect(api_tables(), SEEDS, LAST_SALE)
    for identity in registry.identities.values():
        _, failures = check_identity(con, registry, identity, "fct_orders")
        assert failures, identity.id


def test_the_product_identities_are_undefined_without_orders():
    con = connect(api_tables(), SEEDS, LAST_SALE)
    window = resolve("L3M", Calendar.from_db(con)).current  # Jul to Sep: no sales
    assert check_identity(con, REG, REG.identity("aov_basket"), "fct_orders", window) == (0, [])


@pytest.mark.skipif(not REAL_RUN.is_dir(), reason="needs the real extraction run (kept out of git)")
def test_the_worked_example_of_the_design_note():
    db = open_run(REAL_RUN, Path("config"), date(2026, 11, 22))
    w = resolve("L6W", Calendar.from_db(db.con))
    metrics = ["net_sales", "orders", "items_sold", "aov", "items_per_order", "avg_item_price"]
    rows = [
        evaluate(db.con, REG, "fct_orders", metrics, start=x.start, end=x.end).iloc[0]
        for x in (w.comparison, w.current)
    ]
    assert [r[["net_sales", "orders", "items_sold"]].tolist() for r in rows] == [
        [27_657_250, 412, 799],
        [63_837_875, 857, 1_754],
    ]
    assert [round(r["aov"]) for r in rows] == [67_129, 74_490]
    assert [round(r["items_per_order"], 2) for r in rows] == [1.94, 2.05]
    assert [round(r["avg_item_price"]) for r in rows] == [34_615, 36_396]
