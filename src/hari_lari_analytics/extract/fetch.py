"""Fetch the Moka catalog and transactions and turn them into flat tables."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from hari_lari_analytics.extract.client import MokaClient
from hari_lari_analytics.extract.tables import Table

CATALOG_TABLES = [
    "items", "item_variants", "item_active_modifiers", "categories",
    "discounts", "modifiers", "modifier_options",
]  # fmt: skip
TRANSACTION_TABLES = [
    "transactions", "transaction_lines", "transaction_line_discounts",
    "transaction_line_modifiers", "transaction_line_gratuities",
    "transaction_payment_discounts", "transaction_payment_taxes",
    "transaction_payment_gratuities", "transaction_refunds",
]  # fmt: skip


def outlets(client: MokaClient) -> tuple[int, list[int]]:
    biz = client.get("/v1/businesses")["data"]
    biz = biz.get("businesses") or [biz["business"]]
    bid = biz[0]["id"]
    d = client.get(f"/v1/businesses/{bid}/outlets")["data"]
    outs = d if isinstance(d, list) else d["outlets"]
    return bid, [o["id"] for o in outs]


def fetch_transactions(
    client: MokaClient,
    outlet: int,
    since_ts: float,
    until_ts: float,
    per_page: int = 100,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """v4 get_latest_transactions, cursor-paged via next_url (unix seconds)."""
    first = {
        "per_page": per_page, "include_promo": "true", "reorder_type": "DESC",
        "since": f"{since_ts:.3f}", "until": f"{until_ts:.3f}",
    }  # fmt: skip
    page = client.get(f"/v4/outlets/{outlet}/reports/get_latest_transactions", first)
    seen: dict[str, dict] = {}
    n = 0
    while True:
        n += 1
        d = page["data"]
        for p in d["payments"]:
            seen[p["id"]] = p
        log(f"    page {n}: +{len(d['payments'])} (total {len(seen)})")
        if d.get("completed") or not d.get("next_url") or not d["payments"]:
            break
        page = client.get(d["next_url"])
    return list(seen.values())


def fetch_paged(
    client: MokaClient,
    path: str,
    params: dict,
    label: str,
    per_page: int = 100,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Generic page=N pager; stops on an empty page or when a page adds no new ids."""
    seen: dict = {}
    pg = 1
    while True:
        d = client.get(path, {**params, "per_page": per_page, "page": pg})["data"]
        lst = next((v for v in d.values() if isinstance(v, list)), []) if isinstance(d, dict) else d
        new = 0
        for r in lst:
            key = r.get("id", json.dumps(r, sort_keys=True))
            if key not in seen:
                seen[key] = r
                new += 1
        log(f"    {label} page {pg}: {len(lst)} rows, {new} new")
        if not lst or new == 0 or len(lst) < per_page:
            break
        pg += 1
    return list(seen.values())


def build_catalog(
    client: MokaClient,
    outs: list[int],
    raw: Callable[[str, object], None],
    log: Callable[[str], None] = print,
) -> dict[str, Table]:
    t = {k: Table() for k in CATALOG_TABLES}
    for o in outs:
        log(f"  catalog outlet {o}")
        inc = {"include_deleted": "true"}
        items = fetch_paged(client, f"/v1/outlets/{o}/items", inc, "items", log=log)
        cats = fetch_paged(client, f"/v1/outlets/{o}/categories", inc, "categories", log=log)
        discs = fetch_paged(client, f"/v1/outlets/{o}/discounts", inc, "discounts", log=log)
        mods = fetch_paged(client, f"/v1/outlets/{o}/modifiers", inc, "modifiers", log=log)
        raw(f"items_{o}", items)
        raw(f"categories_{o}", cats)
        raw(f"discounts_{o}", discs)
        raw(f"modifiers_{o}", mods)
        for it in items:
            t["items"].add(it)
            for v in it.get("item_variants", []):
                t["item_variants"].add(
                    v, item_id=it["id"], item_name=it["name"], category_id=it.get("category_id")
                )
            for am in it.get("active_modifiers", []) or []:
                t["item_active_modifiers"].add(
                    am if isinstance(am, dict) else {"modifier_id": am}, item_id=it["id"]
                )
        for c in cats:
            t["categories"].add(c)
        for d in discs:
            t["discounts"].add(d)
        for m in mods:
            t["modifiers"].add(m)
            opts = m.get("active_options") or m.get("modifier_options") or m.get("options") or []
            for opt in opts:
                t["modifier_options"].add(opt, modifier_id=m["id"], modifier_name=m.get("name"))
    return t


def tables_from_payments(payments: list[dict]) -> dict[str, Table]:
    """Flatten payments into the transaction tables. Parent keys win over child fields."""
    T = {k: Table() for k in TRANSACTION_TABLES}
    for p in payments:
        T["transactions"].add(p)
        key = {"payment_id": p["id"], "payment_no": p.get("payment_no")}
        for d in p.get("payment_discounts") or []:
            T["transaction_payment_discounts"].add(d, **key)
        for d in p.get("payment_taxes") or []:
            T["transaction_payment_taxes"].add(d, **key)
        for d in p.get("payment_gratuities") or []:
            T["transaction_payment_gratuities"].add(d, **key)
        for d in p.get("payment_refunds") or []:
            T["transaction_refunds"].add(d, **key)
        for ln in p.get("checkouts") or []:
            T["transaction_lines"].add(ln, **key)
            lk = {**key, "line_uuid": ln.get("uuid")}
            for d in ln.get("discounts") or []:
                T["transaction_line_discounts"].add(d, **lk)
            for d in ln.get("modifiers") or []:
                T["transaction_line_modifiers"].add(d, **lk)
            for d in ln.get("gratuities") or []:
                T["transaction_line_gratuities"].add(d, **lk)
    return T


def build_transactions(
    client: MokaClient,
    outs: list[int],
    since: float,
    until: float,
    raw: Callable[[str, object], None],
    log: Callable[[str], None] = print,
) -> dict[str, Table]:
    payments: list[dict] = []
    for o in outs:
        log(
            f"  transactions outlet {o}: {datetime.fromtimestamp(since):%F} -> "
            f"{datetime.fromtimestamp(until):%F}"
        )
        payments += fetch_transactions(client, o, since, until, log=log)
    raw("transactions", payments)
    return tables_from_payments(payments)
