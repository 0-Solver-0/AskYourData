"""Enrichissement par le 30B : message envoyé, réponse appliquée, contrôle des doublons (sans réseau)."""

import json

from api_service import llm
from tests.test_llm import ENV, PROFILES_PATH, FakeTransport, ok_body
from tools import enrich_with_spark as ews

TABLE = {
    "table_name": "account_move", "odoo_model": "account.move", "odoo_label": "Journal Entry", "row_count": 40,
    "referenced_by": ["account_move_line.move_id"],
    "enrich": {"label": "Invoices, bills and journal entries", "description": None, "synonyms": "invoice",
               "domain": "accounting"},
    "columns": [
        {"column_name": "partner_id", "odoo_type": "many2one", "odoo_label": "Partner", "odoo_help": None,
         "values": None, "references": "res_partner.id",
         "enrich": {"label": "Customer or vendor", "synonyms": "customer, vendor", "notes": "Old note.",
                    "always_include": False}},
        {"column_name": "ref", "odoo_type": "char", "odoo_label": "Reference", "odoo_help": None, "values": None,
         "references": None, "enrich": {"label": None, "synonyms": None, "notes": None, "always_include": False}},
    ],
}

ANSWER = {
    "table": {"label": "Invoices and vendor bills", "description": "One row per invoice or bill.",
              "synonyms": ["Invoice", "vendor bill", "invoice"], "domain": "Accounting"},
    "columns": {"partner_id": {"label": "Partner of the document", "synonyms": ["vendor of the bill",
                                                                              "customer of the invoice"],
                               "notes": None, "always_include": False},
                "ghost": {"label": "x"}},
}


def test_message_keeps_the_facts_and_drops_empty_fields():
    text = ews.table_message(TABLE, TABLE["columns"], columns_only=False)
    head, body = text.split("\n", 1)
    facts = json.loads(body)
    assert head.startswith("Table and columns")
    assert facts["odoo_label"] == "Journal Entry" and facts["current"]["synonyms"] == "invoice"
    assert facts["columns"][1] == {"name": "ref", "type": "char", "odoo_label": "Reference",
                                   "current": {"always_include": False}}
    assert "current" not in json.loads(ews.table_message(TABLE, [], columns_only=True).split("\n", 1)[1])


def test_answer_fills_enrich_blocks_and_keeps_old_notes_when_null():
    report = []
    table = json.loads(json.dumps(TABLE))
    ews.apply_answer(table, ANSWER, report)
    assert table["enrich"] == {"label": "Invoices and vendor bills", "description": "One row per invoice or bill.",
                               "synonyms": "invoice, vendor bill", "domain": "accounting"}
    partner = table["columns"][0]["enrich"]
    assert partner["synonyms"] == "vendor of the bill, customer of the invoice"
    assert partner["notes"] == "Old note."                    # null du modèle : la note juste est gardée
    assert table["columns"][1]["enrich"]["label"] is None     # colonne non citée : inchangée
    assert report == ["account_move : colonne inconnue « ghost » ignorée"]


def test_duplicate_synonyms_between_tables_are_reported():
    tables = [{"table_name": "sale_order", "enrich": {"label": "Sales orders", "synonyms": "quotation, order"}},
              {"table_name": "purchase_order", "enrich": {"label": "Purchase orders", "synonyms": "orders, rfq"}},
              {"table_name": "sale_report", "enrich": {"label": None, "synonyms": "sales order"}}]
    assert ews.duplicate_synonyms(tables) == {"order": ["purchase_order", "sale_order"],
                                              "sale order": ["sale_order", "sale_report"]}


def test_enrich_table_calls_the_model_per_chunk_of_columns(monkeypatch):
    monkeypatch.setattr(ews, "COLUMNS_PER_CALL", 1)
    answers = [ANSWER, {"table": None, "columns": {"ref": {"label": "Bill reference number",
                                                          "synonyms": ["bill reference number"]}}}]
    transport = FakeTransport(*[(200, ok_body(text=json.dumps(a)), "") for a in answers])
    client = llm.LLMClient(llm.load_profiles(PROFILES_PATH), llm.roles_from_env(ENV), env=ENV,
                           transport=transport, retry_delay_s=0)
    table, usage = ews.enrich_table(client, "PROMPT", TABLE, "prep", None)
    assert usage["calls"] == 2 and usage["cost_usd"] > 0
    assert usage["errors"] == ["account_move : colonne inconnue « ghost » ignorée"]
    assert table["columns"][1]["enrich"]["label"] == "Bill reference number"
    assert table["enrich"]["label"] == "Invoices and vendor bills"
    second = transport.calls[1]["payload"]["messages"][1]["content"]
    assert second.startswith("Columns only")
    assert TABLE["enrich"]["label"] == "Invoices, bills and journal entries"   # l'export d'origine n'est pas modifié
