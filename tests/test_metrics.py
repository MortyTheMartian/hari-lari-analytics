import pandas as pd

from hari_lari_analytics.metrics import category_penetration, compute_metrics


def test_totals(lines):
    total = compute_metrics(lines).iloc[0]
    assert total.gross_sales == 263_000
    assert total.discounts == 10_000
    assert total.refunds == 40_000
    assert total.net_sales == 213_000
    assert total.orders == 4
    assert total.aov == 213_000 / 4
    assert total.items_sold == 7
    assert total.first_sale_ts == pd.Timestamp("2026-10-05 07:00:00")
    assert total.last_sale_ts == pd.Timestamp("2026-10-13 11:00:00")


def test_orders_are_distinct_receipts_per_group(lines):
    by_category = compute_metrics(lines, ["adjusted_category"]).set_index("adjusted_category")
    assert by_category.loc["Coffee", "orders"] == 2
    assert by_category.loc["Mains", "orders"] == 1
    assert by_category.loc["Mains", "items_sold"] == 2


def test_penetration_uses_all_orders_of_the_filter_as_denominator(lines):
    pen = category_penetration(lines).set_index("adjusted_category")
    assert (pen["total_orders"] == 4).all()
    assert pen.loc["Coffee", "penetration"] == 0.5
    assert pen.loc["Mains", "penetration"] == 0.25


def test_penetration_respects_prior_filtering(lines):
    bca = lines[lines["payment_type"] == "BCA"]
    pen = category_penetration(bca).set_index("adjusted_category")
    assert pen.loc["Coffee", "total_orders"] == 3
    assert pen.loc["Coffee", "penetration"] == 1 / 3


def test_empty_input_gives_zeroes():
    empty = compute_metrics(
        pd.DataFrame(
            columns=[
                "gross_sales",
                "discounts",
                "refunds",
                "net_sales",
                "quantity",
                "receipt_nr",
                "is_refund",
                "counts_as_order",
                "sale_ts",
            ]
        )
    ).iloc[0]
    assert empty.orders == 0
    assert pd.isna(empty.aov)
