"""A tiny API extraction run that describes the same made-up sales as the CSV fixtures.

The CSV fixtures (`tests/fixtures/2026_10_14_*.csv`) and these tables must give the same
cleaned lines; `tests/test_load_api.py` checks that.
"""

from __future__ import annotations

import pandas as pd

CAFE = 1160056


def _items() -> pd.DataFrame:
    rows = [
        # id, name, category, deleted. Item 5 ("Mystery Cake") is not in the catalog at all.
        (1, "Latte", "Coffee", False),
        (2, "Cappucino", "Coffee", False),
        (3, "Focaccia Tomato", "Snacks (Focaccia)", False),  # sold as "Tomato Focaccia"
        (4, "Nasi Campur", "Mains", False),
        (6, "Custom Amount", "Uncategorized", False),
    ]
    return pd.DataFrame(rows, columns=["id", "name", "category.name", "is_deleted"])


# payment: (id, receipt, created_at, parent id, payment type, refund amount)
_PAYMENTS = [
    ("p5", "R005", "2026-10-13 11:00:00", "p5", "BCA", 0),
    ("p4", "R004", "2026-10-12 10:00:00", "p4", "BCA", 0),
    ("p3", "R003", "2026-10-06 09:00:00", "p3", "Gopay", 0),
    ("p3r", "R003", "2026-10-06 09:00:30", "p3", "Gopay", 40000),
    ("p2", "R002", "2026-10-05 08:00:00", "p2", "Cash", 0),
    ("p1", "R001", "2026-10-05 07:00:00", "p1", "BCA", 0),
]

# line: (payment, uuid, item id, name, variant, category, quantity, gross, discount)
_LINES = [
    ("p5", "u1", 6, "Custom Amount", "", "Uncategorized", 1, 15000, 0),
    ("p4", "u2", 4, "Nasi Campur", "", "Mains", 1, 35000, 0),
    ("p4", "u3", 4, "Nasi Campur", "", "Mains", 1, 35000, 0),
    ("p4", "u4", 5, "Mystery Cake", "", "Cakes", 1, 30000, 0),
    ("p3", "u5", 2, "Cappucino", "Hot", "Coffee", 1, 40000, 0),
    # Moka repeats the refunded line on the refund payment, with positive amounts
    ("p3r", "u6", 2, "Cappucino", "Hot", "Coffee", 1, 40000, 0),
    ("p2", "u7", 1, "Latte", "cold", "Coffee", 1, 40000, 10000),
    ("p1", "u8", 1, "Latte", "Hot", "Uncategorized", 1, 40000, 0),
    ("p1", "u9", 3, "Tomato Focaccia", "", "Focaccia", 1, 28000, 0),
]


def api_tables() -> dict[str, pd.DataFrame]:
    payments = pd.DataFrame(
        _PAYMENTS,
        columns=["id", "payment_no", "created_at", "parent_payment_id", "payment_type_label",
                 "refund_amount"],
    )  # fmt: skip
    payments["created_at"] = pd.to_datetime(payments["created_at"])
    by_id = payments.set_index("id")["payment_no"]

    lines = pd.DataFrame(
        _LINES,
        columns=["payment_id", "uuid", "item_id", "item_name", "item_variant_name",
                 "category_name", "quantity", "gross_sales", "discount_amount"],
    )  # fmt: skip
    lines["payment_no"] = lines["payment_id"].map(by_id)
    lines["net_sales"] = lines["gross_sales"] - lines["discount_amount"]
    lines["item_variant_id"] = lines["item_id"] * 10
    lines["category_id"] = lines["category_name"].map(
        {c: i for i, c in enumerate(sorted(lines["category_name"].unique()), start=100)}
    )
    for column in ["gross_sales", "discount_amount", "net_sales"]:
        lines[column] = lines[column].astype(float)
    lines["note"] = lines["uuid"].map({"u7": "oat milk please"})

    discounts = pd.DataFrame(
        [("p2", "R002", "u7", "FOUNDER25", 10000.0)],
        columns=["payment_id", "payment_no", "line_uuid", "discount_name", "discount_amount"],
    )
    # u9 has two modifiers (listed out of order on purpose), u7 has none
    modifiers = pd.DataFrame(
        [
            ("p1", "R001", "u9", 902, 9021, "Egg Preference", "Poached", 0),
            ("p1", "R001", "u9", 901, 9012, "Breakfast Add-ons", "Chicken", 20000),
        ],
        columns=["payment_id", "payment_no", "line_uuid", "modifier_id", "modifier_option_id",
                 "modifier_name", "modifier_option_name", "price"],
    )  # fmt: skip
    refunds = pd.DataFrame(
        [("p3r", "R003", "Accidental Charge")],
        columns=["payment_id", "payment_no", "refunded_reason"],
    )
    return {
        "transactions": payments,
        "transaction_lines": lines,
        "transaction_line_discounts": discounts,
        "transaction_line_modifiers": modifiers,
        "transaction_refunds": refunds,
        "items": _items(),
    }
