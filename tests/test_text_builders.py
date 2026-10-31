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


def test_enriched_table_is_encoded_as_label_and_each_synonym():
    from schema_service.text_builders import table_vector_texts
    table = {"table_name": "sale_order", "label": "Sales orders and quotations",
             "description": "Customer quotations and confirmed sales orders, one row per order.",
             "synonyms": "sales order, quotation,  Quote ,quotation, Sales orders and quotations",
             "odoo_label": "Sales Order", "domain": "sales"}
    assert table_vector_texts(table, []) == [("label", "Sales orders and quotations"), ("synonym", "sales order"),
                                             ("synonym", "quotation"), ("synonym", "Quote")]
    # Synonymes seuls : pas de libellé Odoo ajouté (« Journal Entry » pour account_move)
    only = {"table_name": "account_move", "synonyms": "vendor bill", "odoo_label": "Journal Entry"}
    assert table_vector_texts(only, []) == [("synonym", "vendor bill")]


def test_table_without_label_or_synonyms_keeps_the_full_card():
    from schema_service.text_builders import build_table_text, table_vector_texts
    table = {"table_name": "stock_quant", "odoo_label": "Quants", "description": "Stock on hand."}
    texts = table_vector_texts(table, [])
    assert texts == [("card", build_table_text({**table, "description": None}, []))]
    assert "Stock on hand" not in texts[0][1]                     # description jamais encodée


def test_column_with_only_a_note_keeps_its_note_out_of_the_vector():
    from schema_service.text_builders import column_vector_texts
    col = {"column_name": "payment_reference", "odoo_label": "Payment Reference",
           "odoo_help": "The payment reference to set on journal items.",
           "notes": "Communication text for the payment; not a journal and not an amount."}
    (source, text), = column_vector_texts(col)
    assert source == "card" and "Payment Reference" in text and "journal items" in text
    assert "not a journal" not in text


def test_enriched_column_leaves_notes_and_help_out_of_the_vectors():
    from schema_service.text_builders import build_column_text, column_vector_texts
    col = {"column_name": "partner_id", "label": None, "odoo_label": "Partner",
           "synonyms": "vendor of the bill, customer of the invoice",
           "notes": "Customer on invoices, vendor on bills.", "odoo_help": "Partner", "selection_values": None}
    assert column_vector_texts(col) == [("label", "Partner"), ("synonym", "vendor of the bill"),
                                        ("synonym", "customer of the invoice")]
    bare = {"column_name": "state", "odoo_label": "Status", "selection_values": "draft=Draft"}
    assert column_vector_texts(bare) == [("card", build_column_text(bare))]
