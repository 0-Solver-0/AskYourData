"""Tests de construction des textes encodés (sans modèle)."""

from schema_service.text_builders import (
    build_column_text,
    build_table_text,
    column_display_label,
    format_selection,
)


def test_format_selection():
    assert format_selection([("draft", "Quotation"), ("sale", "Sales Order")]) == \
        "draft=Quotation; sale=Sales Order"
    assert format_selection([("a", "a"), ("b", None)]) == "a; b"
    assert format_selection([]) is None


def test_format_selection_is_capped():
    text = format_selection([(str(i), f"v{i}") for i in range(50)])
    assert text.endswith("…")
    assert text.count(";") == 20


def test_label_priority_manual_then_odoo_then_name():
    assert column_display_label({"column_name": "amount_total", "label": "Total", "odoo_label": "Total TTC"}) == "Total"
    assert column_display_label({"column_name": "amount_total", "odoo_label": "Total TTC"}) == "Total TTC"
    assert column_display_label({"column_name": "amount_total"}) == "amount total"


def test_column_text_contains_labels_and_values():
    text = build_column_text({
        "column_name": "state", "odoo_label": "Status", "odoo_help": "Order status",
        "selection_values": "draft=Quotation; sale=Sales Order",
    })
    assert "Status" in text and "Order status" in text and "draft=Quotation" in text


def test_table_text_starts_with_most_useful_information():
    table = {"table_name": "account_move", "odoo_model": "account.move", "odoo_label": "Journal Entry",
             "description": "Customer invoices and vendor bills"}
    cols = [
        {"column_name": "id"},
        {"column_name": "create_uid"},
        {"column_name": "amount_residual", "odoo_label": "Amount Due"},
    ]
    text = build_table_text(table, cols)
    assert text.startswith("account_move | account move | account.move | Journal Entry")
    assert "Customer invoices" in text
    assert "Amount Due" in text
    assert "create uid" not in text  # colonnes techniques ignorées
