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


def test_enriched_table_text_reads_like_a_subject():
    table = {"table_name": "account_move", "odoo_model": "account.move", "odoo_label": "Journal Entry",
             "label": "Invoices and bills", "description": "Customer invoices and vendor bills",
             "synonyms": "invoice, bill", "domain": "accounting"}
    cols = [{"column_name": "id"}, {"column_name": "amount_residual", "odoo_label": "Amount Due"}]
    text = build_table_text(table, cols)
    assert text.startswith("Invoices and bills | Customer invoices and vendor bills | invoice, bill | Journal Entry")
    assert "Amount Due" not in text            # table enrichie : les colonnes ont leurs propres vecteurs
    assert "domain: accounting" in text


def test_plain_table_text_lists_business_columns_first():
    table = {"table_name": "account_move", "odoo_label": "Journal Entry"}
    cols = [
        {"column_name": "id"},
        {"column_name": "create_uid"},
        {"column_name": "sequence_prefix", "odoo_label": "Sequence Prefix", "odoo_type": "char"},
        {"column_name": "payment_state", "odoo_label": "Payment Status", "odoo_type": "selection"},
        {"column_name": "amount_residual", "odoo_label": "Amount Due", "odoo_type": "monetary"},
    ]
    text = build_table_text(table, cols)
    assert text.startswith("Journal Entry | account move")
    assert text.index("Payment Status") < text.index("Amount Due") < text.index("Sequence Prefix")
    assert "create uid" not in text


def test_column_text_puts_meaning_before_technical_name():
    text = build_column_text({"column_name": "amount_residual_signed", "odoo_label": "Amount Due Signed",
                              "synonyms": "amount due", "notes": "Negative for credit notes"})
    assert text.startswith("Amount Due Signed | amount due | Negative for credit notes")
    assert text.endswith("column amount_residual_signed")
