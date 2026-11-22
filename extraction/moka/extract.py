"""Full extraction from the Moka API into flat, id-rich CSV tables (+ raw JSON).

Run (from repo root):
    uv run python extraction/moka/extract.py                 # full history
    uv run python extraction/moka/extract.py --since 2026-11-01   # incremental transactions
    uv run python extraction/moka/extract.py --only catalog       # items/categories/discounts/modifiers

Output (extraction/moka/output/extract/<run date>/):
    raw/*.json                 untouched API responses (all pages, concatenated)
    transactions.csv           one row per payment (receipt)
    transaction_lines.csv      one row per checkout line  (item_id, item_variant_id, category_id, ...)
    transaction_line_discounts.csv, transaction_line_modifiers.csv, transaction_payment_discounts.csv ...
    items.csv, item_variants.csv, categories.csv, discounts.csv, modifiers.csv, modifier_options.csv
    request_log.csv            every HTTP call: time, status, bytes, seconds (= cost / rate-limit evidence)
Nested objects become dotted columns; arrays of objects become child tables carrying the parent keys.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import moka_client as moka  # noqa: E402

OUT_ROOT = Path(__file__).parent / "output" / "extract"
LOG: list[list] = []
MIN_INTERVAL = 0.0  # seconds between calls; raise if the API ever answers 429


def call(url_or_path: str, params: dict | None = None, retries: int = 5):
    """GET with retry/backoff on 429/5xx. Logs every attempt."""
    url = url_or_path if url_or_path.startswith("http") else moka.BASE + url_or_path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    if "access_token" in q:  # next_url carries an empty token; auth goes in the header
        q.pop("access_token")
        url = parsed._replace(query=urllib.parse.urlencode(q, doseq=True)).geturl()
    for attempt in range(retries):
        time.sleep(MIN_INTERVAL)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {moka.access_token()}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, body, hdr = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, body, hdr = e.code, e.read(), e.headers
        except (urllib.error.URLError, TimeoutError):
            status, body, hdr = 0, b"", {}
        dt = time.time() - t0
        LOG.append([datetime.now().isoformat(timespec="seconds"), parsed.path, status, len(body),
                    f"{dt:.2f}", hdr.get("Retry-After", "") if hdr else ""])
        if status == 200:
            return json.loads(body)
        if status in (0, 429, 500, 502, 503, 504):
            wait = float(hdr.get("Retry-After", 0) or 0) if hdr else 0
            time.sleep(max(wait, 2**attempt))
            continue
        raise RuntimeError(f"{status} {url}: {body[:300]!r}")
    raise RuntimeError(f"gave up after {retries} attempts: {url}")


# ---------------------------------------------------------------- flattening
def flat(rec: dict, prefix: str = "") -> dict:
    """Nested dicts -> dotted columns; lists are dropped (handled as child tables) except scalar lists."""
    out = {}
    for k, v in rec.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                continue
            out[key] = json.dumps(v) if v else ""
        else:
            out[key] = v
    return out


class Table:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, rec: dict, **parent):
        self.rows.append({**parent, **flat(rec)})

    def write(self, path: Path):
        cols: list[str] = []
        for r in self.rows:
            for c in r:
                if c not in cols:
                    cols.append(c)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(self.rows)
        print(f"  {path.name}: {len(self.rows)} rows x {len(cols)} cols")


# ---------------------------------------------------------------- fetchers
def outlets() -> tuple[int, list[int]]:
    biz = call("/v1/businesses")["data"]
    biz = biz.get("businesses") or [biz["business"]]
    bid = biz[0]["id"]
    d = call(f"/v1/businesses/{bid}/outlets")["data"]
    outs = d if isinstance(d, list) else d["outlets"]
    return bid, [o["id"] for o in outs]


def fetch_transactions(outlet: int, since_ts: float, until_ts: float, per_page: int = 100) -> list[dict]:
    """v4 get_latest_transactions, cursor-paged via next_url (since/until are unix seconds)."""
    first = {"per_page": per_page, "include_promo": "true", "reorder_type": "DESC",
             "since": f"{since_ts:.3f}", "until": f"{until_ts:.3f}"}
    page = call(f"/v4/outlets/{outlet}/reports/get_latest_transactions", first)
    seen: dict[str, dict] = {}
    n = 0
    while True:
        n += 1
        d = page["data"]
        for p in d["payments"]:
            seen[p["id"]] = p
        print(f"    page {n}: +{len(d['payments'])} (total {len(seen)})")
        if d.get("completed") or not d.get("next_url") or not d["payments"]:
            break
        page = call(d["next_url"])
    return list(seen.values())


def fetch_paged(path: str, params: dict, label: str, per_page: int = 100) -> list[dict]:
    """Generic page=N pager; stops on empty page or when a page adds no new ids."""
    seen: dict = {}
    pg = 1
    while True:
        d = call(path, {**params, "per_page": per_page, "page": pg})["data"]
        lst = next((v for v in d.values() if isinstance(v, list)), []) if isinstance(d, dict) else d
        new = 0
        for r in lst:
            key = r.get("id", json.dumps(r, sort_keys=True))
            if key not in seen:
                seen[key] = r
                new += 1
        print(f"    {label} page {pg}: {len(lst)} rows, {new} new")
        if not lst or new == 0 or len(lst) < per_page:
            break
        pg += 1
    return list(seen.values())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD, transactions from this date (default 2026-01-01)")
    ap.add_argument("--until", help="YYYY-MM-DD exclusive (default now)")
    ap.add_argument("--only", choices=["transactions", "catalog"], help="limit to one group")
    ap.add_argument("--out", help="output dir override")
    a = ap.parse_args()

    out = Path(a.out) if a.out else OUT_ROOT / date.today().isoformat()
    (out / "raw").mkdir(parents=True, exist_ok=True)
    tz = datetime.now().astimezone().tzinfo
    since = datetime.fromisoformat(a.since or "2026-01-01").replace(tzinfo=tz).timestamp()
    until = datetime.fromisoformat(a.until).replace(tzinfo=tz).timestamp() if a.until else time.time()

    biz, outs = outlets()
    print(f"business {biz}, outlets {outs}")
    raw = lambda name, obj: (out / "raw" / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False))

    if a.only in (None, "catalog"):
        t_items, t_vars, t_cat, t_disc, t_mod, t_opt, t_active = (Table() for _ in range(7))
        for o in outs:
            print(f"  catalog outlet {o}")
            inc = {"include_deleted": "true"}
            items = fetch_paged(f"/v1/outlets/{o}/items", inc, "items")
            cats = fetch_paged(f"/v1/outlets/{o}/categories", inc, "categories")
            discs = fetch_paged(f"/v1/outlets/{o}/discounts", inc, "discounts")
            mods = fetch_paged(f"/v1/outlets/{o}/modifiers", inc, "modifiers")
            raw(f"items_{o}", items); raw(f"categories_{o}", cats)
            raw(f"discounts_{o}", discs); raw(f"modifiers_{o}", mods)
            for it in items:
                t_items.add(it)
                for v in it.get("item_variants", []):
                    t_vars.add(v, item_id=it["id"], item_name=it["name"], category_id=it.get("category_id"))
                for am in it.get("active_modifiers", []) or []:
                    t_active.add(am if isinstance(am, dict) else {"modifier_id": am}, item_id=it["id"])
            for c in cats: t_cat.add(c)
            for d in discs: t_disc.add(d)
            for m in mods:
                t_mod.add(m)
                for opt in (m.get("active_options") or m.get("modifier_options") or m.get("options") or []):
                    t_opt.add(opt, modifier_id=m["id"], modifier_name=m.get("name"))
        for name, t in [("items", t_items), ("item_variants", t_vars), ("item_active_modifiers", t_active),
                        ("categories", t_cat), ("discounts", t_disc), ("modifiers", t_mod),
                        ("modifier_options", t_opt)]:
            t.write(out / f"{name}.csv")

    if a.only in (None, "transactions"):
        T = {k: Table() for k in ["transactions", "transaction_lines", "transaction_line_discounts",
                                  "transaction_line_modifiers", "transaction_line_gratuities",
                                  "transaction_payment_discounts", "transaction_payment_taxes",
                                  "transaction_payment_gratuities", "transaction_refunds"]}
        all_payments = []
        for o in outs:
            print(f"  transactions outlet {o}: {datetime.fromtimestamp(since):%F} -> {datetime.fromtimestamp(until):%F}")
            payments = fetch_transactions(o, since, until)
            all_payments += payments
            for p in payments:
                pid = p["id"]
                T["transactions"].add(p)
                key = {"payment_id": pid, "payment_no": p.get("payment_no")}
                for d in p.get("payment_discounts") or []: T["transaction_payment_discounts"].add(d, **key)
                for d in p.get("payment_taxes") or []: T["transaction_payment_taxes"].add(d, **key)
                for d in p.get("payment_gratuities") or []: T["transaction_payment_gratuities"].add(d, **key)
                for d in p.get("payment_refunds") or []: T["transaction_refunds"].add(d, **key)
                for ln in p.get("checkouts") or []:
                    T["transaction_lines"].add(ln, **key)
                    lk = {**key, "line_uuid": ln.get("uuid")}
                    for d in ln.get("discounts") or []: T["transaction_line_discounts"].add(d, **lk)
                    for d in ln.get("modifiers") or []: T["transaction_line_modifiers"].add(d, **lk)
                    for d in ln.get("gratuities") or []: T["transaction_line_gratuities"].add(d, **lk)
        raw("transactions", all_payments)
        for name, t in T.items():
            t.write(out / f"{name}.csv")

    with open(out / "request_log.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "path", "status", "bytes", "seconds", "retry_after"])
        w.writerows(LOG)
    tot = sum(r[3] for r in LOG)
    print(f"\n{len(LOG)} HTTP calls, {tot/1e6:.1f} MB, {sum(float(r[4]) for r in LOG):.0f}s -> {out}")


if __name__ == "__main__":
    main()
