"""Call the Moka REST endpoints once each, keep the very first record, and write:

  output/raw/<table>.json        raw first JSON record (+ response envelope keys)
  output/schema.csv              information_schema.columns-like dump with an example value
  output/probe_log.csv           status per endpoint (errors, empty results)

Run: uv run python extraction/moka/probe_schema.py  (from the repo root)
"""

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from hari_lari_analytics.extract.cli import DEFAULT_TOKEN_FILE, load_env
from hari_lari_analytics.extract.tokens import BASE, FileTokenStore, TokenManager

load_env()
_tokens = TokenManager(FileTokenStore(DEFAULT_TOKEN_FILE))


class moka:  # noqa: N801 - tiny shim keeping the old call sites unchanged
    @staticmethod
    def get(path, params=None):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {_tokens.access_token()}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, body = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode()
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, body

OUT = Path(__file__).parent / "output"
RAW = OUT / "raw"
RAW.mkdir(parents=True, exist_ok=True)

today = date.today()
START = (today - timedelta(days=60)).strftime("%d/%m/%Y")
END = today.strftime("%d/%m/%Y")

# (table_name, path template, params, needs)  needs: None | "business" | "outlet"
EP = [
    ("profile", "/v1/profile/self", {}, None),
    ("businesses", "/v1/businesses", {}, None),
    ("outlets", "/v1/businesses/{business_id}/outlets", {}, "business"),
    ("quotas", "/v1/quotas", {}, None),
    ("customers", "/v1/businesses/{business_id}/customers", {"per_page": 1}, "business"),
    ("items", "/v1/outlets/{outlet_id}/items", {"per_page": 1, "include_deleted": "true"}, "outlet"),
    ("categories", "/v1/outlets/{outlet_id}/categories", {"per_page": 1, "include_deleted": "true"}, "outlet"),
    ("modifiers", "/v1/outlets/{outlet_id}/modifiers", {"per_page": 1, "include_deleted": "true"}, "outlet"),
    ("discounts", "/v1/outlets/{outlet_id}/discounts", {"per_page": 1}, "outlet"),
    ("taxes", "/v1/outlets/{outlet_id}/taxes", {"per_page": 1}, "outlet"),
    ("gratuities", "/v1/outlets/{outlet_id}/gratuities", {"per_page": 1}, "outlet"),
    ("promos", "/v1/outlets/{outlet_id}/promos", {"per_page": 1}, "outlet"),
    ("favorites", "/v1/outlets/{outlet_id}/favorites", {}, "outlet"),
    ("bundle_package", "/v1/outlets/{outlet_id}/bundle_package", {"per_page": 1}, "outlet"),
    ("ingredients_library", "/v1/outlets/{outlet_id}/ingredients/library", {"per_page": 1}, "outlet"),
    ("ingredients_categories", "/v1/outlets/{outlet_id}/ingredients/categories", {"per_page": 1}, "outlet"),
    ("sales_type", "/v1/outlets/{outlet_id}/sales_type", {}, "outlet"),
    ("inventory_item_summaries", "/v1/outlets/{outlet_id}/inventory/item_summaries", {"per_page": 1}, "outlet"),
    ("purchase_orders", "/v1/outlets/{outlet_id}/inventory/purchase_orders", {"per_page": 1}, "outlet"),
    ("checkout_settings", "/v1/businesses/{business_id}/checkout_settings", {}, "business"),
    ("loyalty_programs", "/v1/businesses/{business_id}/loyalties/programs", {}, "business"),
    ("sync_bills", "/v1/outlets/{outlet_id}/sync_bills/", {"per_page": 1}, "outlet"),
    # transactions (row-level sales data)
    ("transactions_v4", "/v4/outlets/{outlet_id}/reports/get_latest_transactions",
     {"per_page": 1, "include_promo": "true", "reorder_type": "DESC"}, "outlet"),
    ("transactions_v3", "/v3/outlets/{outlet_id}/reports/get_latest_transactions", {"per_page": 1}, "outlet"),
    ("invoices_v3", "/v3/outlets/{outlet_id}/reports/invoices", {"per_page": 1}, "outlet"),
    # aggregate reports
    ("report_item_sales_v3", "/v3/outlets/{outlet_id}/reports/item_sales", {"start": START, "end": END}, "outlet"),
    ("report_category_sales", "/v2/outlets/{outlet_id}/reports/category_sales", {"start": START, "end": END}, "outlet"),
    ("report_collector_sales", "/v2/outlets/{outlet_id}/reports/collector_sales", {"start": START, "end": END}, "outlet"),
    ("report_discount_sales", "/v2/outlets/{outlet_id}/reports/discount_sales", {"start": START, "end": END}, "outlet"),
    ("report_gratuity_sales", "/v2/outlets/{outlet_id}/reports/gratuity_sales", {"start": START, "end": END}, "outlet"),
    ("report_modifier_sales", "/v2/outlets/{outlet_id}/reports/modifier_sales", {"start": START, "end": END}, "outlet"),
    ("report_sales_summary", "/v2/outlets/{outlet_id}/reports/sales_summary", {"start": START, "end": END}, "outlet"),
    ("report_server_sales", "/v2/outlets/{outlet_id}/reports/server_sales", {"start": START, "end": END}, "outlet"),
    ("report_payment_methods", "/v2/outlets/{outlet_id}/reports/payment_methods", {"start": START, "end": END}, "outlet"),
    ("report_tax_sales", "/v2/outlets/{outlet_id}/reports/tax_sales", {"start": START, "end": END}, "outlet"),
    ("report_sales_types", "/v1/outlets/{outlet_id}/reports/sales_types", {"start": START, "end": END}, "outlet"),
]


def first_records(payload):
    """Return (envelope_description, list_key, first_record) from a Moka response."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return "data[]", data[0] if data else None
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return f"data.{k}[]", v[0] if v else None
        return "data{}", data
    return "?", None


def type_of(v):
    if v is None: return "null"
    if isinstance(v, bool): return "boolean"
    if isinstance(v, int): return "integer"
    if isinstance(v, float): return "float"
    if isinstance(v, str): return "string"
    return "array" if isinstance(v, list) else "object"


rows = []  # schema rows
logs = []


def flatten(table, rec, prefix="", ctx=None):
    """Nested objects -> dotted columns; arrays of objects -> child table '<table>.<key>'."""
    ctx = ctx if ctx is not None else {"pos": 0}
    for k, v in rec.items():
        col = f"{prefix}{k}"
        if isinstance(v, dict):
            flatten(table, v, col + ".", ctx)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            ctx["pos"] += 1
            rows.append([table, col, ctx["pos"], "array<object>", f"[{len(v)} elements in first row]"])
            child = {"pos": 0}
            flatten(f"{table}.{col}", v[0], "", child)
        else:
            ctx["pos"] += 1
            ex = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            rows.append([table, col, ctx["pos"], type_of(v) if v != [] else "array<empty>", ex[:200]])


def find_ids():
    ids = {}
    s, p = moka.get("/v1/businesses")
    logs.append(["businesses(bootstrap)", s, ""])
    data = p.get("data", {}) if s == 200 else {}
    biz = data.get("businesses") or ([data["business"]] if data.get("business") else [])
    ids["business_id"] = biz[0]["id"] if biz else None
    s, p = moka.get(f"/v1/businesses/{ids['business_id']}/outlets")
    data = p.get("data", {}) if s == 200 else {}
    outs = data if isinstance(data, list) else data.get("outlets") or ([data["outlet"]] if data.get("outlet") else [])
    ids["outlets"] = [o["id"] for o in outs]
    print("business:", ids["business_id"], "outlets:", ids["outlets"])
    return ids


def main():
    ids = find_ids()
    for table, path, params, needs in EP:
        if needs == "outlet":
            targets = ids["outlets"]
        else:
            targets = [None]
        got = None
        for oid in targets:
            p = path.format(outlet_id=oid, business_id=ids["business_id"])
            status, payload = moka.get(p, params)
            note = ""
            if status == 200:
                env, rec = first_records(payload)
                if rec is not None:
                    got = (oid, env, rec, payload)
                    note = f"{env} outlet={oid}"
                    logs.append([table, status, note])
                    break
                note = f"empty {env} outlet={oid}"
            else:
                note = str(payload)[:200].replace("\n", " ")
            logs.append([table, status, note])
        if got:
            oid, env, rec, payload = got
            (RAW / f"{table}.json").write_text(
                json.dumps({"outlet_id": oid, "envelope": env, "first_record": rec}, indent=2, ensure_ascii=False))
            if isinstance(rec, dict):
                flatten(table, rec)
            print(f"OK   {table}: {env}")
        else:
            print(f"--   {table}: {logs[-1][1]} {logs[-1][2][:80]}")

    with open(OUT / "schema.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table_name", "column_name", "ordinal_position", "data_type", "example_value"])
        w.writerows(rows)
    with open(OUT / "probe_log.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "http_status", "note"])
        w.writerows(logs)
    print(f"\n{len(rows)} columns -> {OUT/'schema.csv'}")


if __name__ == "__main__":
    main()
