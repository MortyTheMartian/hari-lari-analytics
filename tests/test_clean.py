def _line(lines, receipt, item):
    return lines[(lines["receipt_nr"] == receipt) & (lines["item"] == item)]


def test_personal_data_is_dropped(cleaned):
    assert not {"Customer", "Collected By", "customer", "collected_by"} & set(cleaned.lines)


def test_refund_points_back_to_refunded_item(cleaned):
    refund = cleaned.lines[cleaned.lines["is_refund"]].iloc[0]
    assert refund["item"] == "Cappucino"
    assert refund["category"] == "Coffee"
    assert refund["quantity"] == -1


def test_fully_refunded_receipt_does_not_count_as_order(cleaned):
    per_receipt = cleaned.lines.groupby("receipt_nr")["counts_as_order"].all()
    assert not per_receipt["R003"]
    assert per_receipt.drop("R003").all()


def test_identical_lines_are_kept(cleaned):
    assert len(_line(cleaned.lines, "R004", "Nasi Campur")) == 2


def test_variant_capitalization_is_normalized(cleaned):
    assert _line(cleaned.lines, "R002", "Latte")["variant"].item() == "Cold"


def test_discount_dimensions(cleaned):
    discounted = cleaned.lines.set_index("receipt_nr")["discounted"]
    assert discounted["R002"]
    assert not discounted.drop("R002").any()
    assert _line(cleaned.lines, "R002", "Latte")["discount_code"].item() == "FOUNDER25"


def test_adjusted_item_and_category(cleaned):
    lines = cleaned.lines
    focaccia = _line(lines, "R001", "Tomato Focaccia")
    assert focaccia["adjusted_item"].item() == "Focaccia Tomato"
    assert focaccia["adjusted_category"].item() == "Snacks (Focaccia)"
    assert _line(lines, "R001", "Latte")["adjusted_category"].item() == "Coffee"
    assert _line(lines, "R005", "Custom Amount")["adjusted_category"].item() == "Custom Amount"


def test_unknown_items_fall_back_to_sales_category_with_a_note(cleaned):
    assert _line(cleaned.lines, "R004", "Mystery Cake")["adjusted_category"].item() == "Cakes"
    assert any("Mystery Cake" in note for note in cleaned.notes)


def test_item_variant_label(lines):
    labels = dict(
        zip(lines["item"] + "|" + lines["receipt_nr"], lines["item_variant"], strict=True)
    )
    assert labels["Latte|R001"] == "Latte-Hot"
    assert labels["Tomato Focaccia|R001"] == "Tomato Focaccia"
    assert set(lines.loc[lines["item"] == "Tomato Focaccia", "adjusted_item_variant"]) == {
        "Focaccia Tomato"
    }
