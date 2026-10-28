"""
Tests de la recherche de schéma (sans base ni modèle).

Les similarités vectorielles sont volontairement presque identiques pour toutes les tables,
comme observé sur la vraie base Odoo avant enrichissement : la sélection doit rester correcte
grâce aux libellés, au vocabulaire métier et au graphe des clés étrangères.
"""

import os

import pytest

from schema_service import search

SYNONYMS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "search_synonyms.yaml")


def col(name, data_type="integer", label=None, *, pk=False, fk=False, odoo_type=None, sel=None, sim=0.2):
    return {"column_name": name, "data_type": data_type, "odoo_label": label, "is_pk": pk, "is_fk": fk,
            "is_nullable": not pk, "odoo_type": odoo_type, "selection_values": sel, "similarity": sim}


COMMON = [col("create_uid", label="Created by", fk=True), col("write_uid", label="Last Updated by", fk=True),
          col("create_date", "timestamp without time zone", "Created on"),
          col("company_id", label="Company", fk=True), col("currency_id", label="Currency", fk=True)]

CATALOG = {
    "res_partner": ("res.partner", "Contact", 62, [
        col("id", pk=True, label="ID"), col("name", "character varying", "Name"),
        col("country_id", label="Country", fk=True), col("customer_rank", label="Customer Rank"),
    ]),
    "res_users": ("res.users", "User", 7, [
        col("id", pk=True, label="ID"), col("login", "character varying", "Login"),
        col("partner_id", label="Related Partner", fk=True),
    ]),
    "sale_order": ("sale.order", "Sales Order", 24, [
        col("id", pk=True, label="ID"), col("name", "character varying", "Order Reference"),
        col("partner_id", label="Customer", fk=True), col("user_id", label="Salesperson", fk=True),
        col("team_id", label="Sales Team", fk=True),
        col("state", "character varying", "Status", sel="draft=Quotation; sale=Sales Order; cancel=Cancelled"),
        col("date_order", "timestamp without time zone", "Order Date", odoo_type="datetime"),
        col("amount_total", "numeric", "Total", odoo_type="monetary"),
        col("amount_untaxed", "numeric", "Untaxed Amount", odoo_type="monetary"),
    ]),
    "sale_order_line": ("sale.order.line", "Sales Order Line", 50, [
        col("id", pk=True, label="ID"), col("order_id", label="Order Reference", fk=True),
        col("product_id", label="Product", fk=True), col("price_subtotal", "numeric", "Subtotal", odoo_type="monetary"),
        col("product_uom_qty", "numeric", "Quantity"),
    ]),
    "account_move": ("account.move", "Journal Entry", 24, [
        col("id", pk=True, label="ID"), col("name", "character varying", "Number"),
        col("journal_id", label="Journal", fk=True, sim=0.5),
        col("origin_payment_id", label="Payment", fk=True, sim=0.5),
        col("tax_cash_basis_rec_id", label="Tax Cash Basis Entry of", fk=True, sim=0.5),
        col("commercial_partner_id", label="Commercial Entity", fk=True, sim=0.5),
        col("partner_shipping_id", label="Delivery Address", fk=True, sim=0.5),
        col("partner_bank_id", label="Recipient Bank", fk=True, sim=0.5),
        col("reversed_entry_id", label="Reversal of", fk=True, sim=0.5),
        col("partner_id", label="Partner", fk=True), col("invoice_user_id", label="Salesperson", fk=True),
        col("move_type", "character varying", "Type", sel="entry=Journal Entry; out_invoice=Customer Invoice"),
        col("payment_state", "character varying", "Payment Status", sel="not_paid=Not Paid; paid=Paid"),
        col("amount_residual", "numeric", "Amount Due", odoo_type="monetary"),
        col("amount_total", "numeric", "Total", odoo_type="monetary"),
        col("invoice_date", "date", "Invoice/Bill Date", odoo_type="date"),
    ]),
    "crm_lead": ("crm.lead", "Lead/Opportunity", 40, [
        col("id", pk=True, label="ID"), col("name", "character varying", "Opportunity"),
        col("stage_id", label="Stage", fk=True), col("user_id", label="Salesperson", fk=True),
        col("expected_revenue", "numeric", "Expected Revenue", odoo_type="monetary"),
        col("probability", "double precision", "Probability"),
    ]),
    "crm_stage": ("crm.stage", "CRM Stages", 4, [
        col("id", pk=True, label="ID"), col("name", "jsonb", "Stage Name", odoo_type="char"),
    ]),
    "crm_team": ("crm.team", "Sales Team", 4, [
        col("id", pk=True, label="ID"), col("name", "jsonb", "Sales Team", odoo_type="char"),
        col("invoiced_target", "double precision", "Invoicing Target"),
    ]),
    "purchase_order": ("purchase.order", "Purchase Order", 11, [
        col("id", pk=True, label="ID"), col("name", "character varying", "Order Reference"),
        col("partner_id", label="Vendor", fk=True, sim=0.5), col("user_id", label="Buyer", fk=True),
        col("invoice_count", label="Bill Count", sim=0.5), col("invoice_status", "character varying", "Billing Status"),
        col("amount_total", "numeric", "Total", odoo_type="monetary", sim=0.5),
    ]),
    "account_payment": ("account.payment", "Payments", 0, [
        col("id", pk=True, label="ID"), col("amount", "numeric", "Amount", odoo_type="monetary"),
        col("partner_id", label="Customer/Vendor", fk=True), col("payment_type", "character varying", "Payment Type"),
    ]),
    "account_journal": ("account.journal", "Journal", 16, [
        col("id", pk=True, label="ID"), col("name", "jsonb", "Journal Name", odoo_type="char"),
    ]),
    "purchase_order_line": ("purchase.order.line", "Purchase Order Line", 23, [
        col("id", pk=True, label="ID"), col("price_subtotal", "numeric", "Subtotal", odoo_type="monetary"),
        col("partner_id", label="Partner", fk=True),
    ]),
    "res_country": ("res.country", "Country", 250, [
        col("id", pk=True, label="ID"), col("name", "jsonb", "Country Name", odoo_type="char"),
        col("code", "character varying", "Country Code"),
    ]),
    "account_tax": ("account.tax", "Tax", 154, [
        col("id", pk=True, label="ID"), col("amount", "numeric", "Amount"), col("name", "jsonb", "Tax Name"),
    ]),
    "utm_stage": ("utm.stage", "Campaign Stage", 4, [
        col("id", pk=True, label="ID"), col("name", "jsonb", "Name"),
    ]),
    "account_analytic_line": ("account.analytic.line", "Analytic Line", 0, [
        col("id", pk=True, label="ID"), col("amount", "numeric", "Amount", odoo_type="monetary"),
        col("partner_id", label="Partner", fk=True),
    ]),
}

RELATIONS = [
    ("sale_order", "partner_id", "res_partner"), ("sale_order", "user_id", "res_users"),
    ("sale_order", "team_id", "crm_team"), ("sale_order_line", "order_id", "sale_order"),
    ("account_move", "partner_id", "res_partner"), ("account_move", "invoice_user_id", "res_users"),
    ("account_move", "commercial_partner_id", "res_partner"), ("account_move", "partner_shipping_id", "res_partner"),
    ("account_move", "journal_id", "account_journal"), ("account_move", "origin_payment_id", "account_payment"),
    ("purchase_order", "partner_id", "res_partner"), ("purchase_order", "user_id", "res_users"),
    ("account_payment", "partner_id", "res_partner"),
    ("crm_lead", "stage_id", "crm_stage"), ("crm_lead", "user_id", "res_users"),
    ("res_users", "partner_id", "res_partner"), ("purchase_order_line", "partner_id", "res_partner"),
    ("account_analytic_line", "partner_id", "res_partner"), ("res_partner", "country_id", "res_country"),
    ("sale_order", "create_uid", "res_users"), ("crm_lead", "company_id", "res_company"),
]


@pytest.fixture(scope="module")
def synonyms():
    return search.load_synonyms(SYNONYMS_PATH)


def build_inputs(vector_scores=None):
    tables_by_name, columns_by_table, candidates = {}, {}, []
    for i, (name, (model, label, rows, cols)) in enumerate(CATALOG.items(), start=1):
        all_cols = cols + COMMON
        tables_by_name[name] = {"table_name": name, "odoo_model": model, "odoo_label": label, "row_count": rows}
        columns_by_table[name] = all_cols
        candidates.append(search.TableCandidate(
            id=i, table_name=name, row_count=rows,
            core_tokens=search.table_core_tokens(tables_by_name[name]),
            column_tokens=search.column_tokens(all_cols),
            vector_score=(vector_scores or {}).get(name, 0.27),
            column_vector_score=0.25,
        ))
    relations = [{"from_table": a, "from_column": c, "to_table": b, "to_column": "id"} for a, c, b in RELATIONS]
    return candidates, tables_by_name, columns_by_table, relations


VALUES = [
    {"table_name": "res_country", "column_name": "name", "value": v, "normalized": search.normalize_text(v)}
    for v in ("Morocco", "France", "Côte d'Ivoire", "United States")
] + [
    {"table_name": "crm_stage", "column_name": "name", "value": v, "normalized": search.normalize_text(v)}
    for v in ("New", "Qualified", "Won")
] + [
    {"table_name": "res_partner", "column_name": "name", "value": "Azure Interior",
     "normalized": "azure interior"},
]


def plan(question, synonyms, params=None, vector_scores=None):
    query = search.build_query(question, synonyms)
    cands, tables, cols, rels = build_inputs(vector_scores)
    matches = search.match_values(question, VALUES)
    return search.plan_schema(query, cands, tables, cols, rels, params or search.PlanParams(), matches)


def names(p):
    return [t["table_name"] for t in p["tables"]]


# ── Vocabulaire ──────────────────────────────────────────────────────────────

def test_tokenize_singular_and_stopwords():
    assert search.tokenize("Top 5 customers by revenue") == ["customer", "revenue"]
    assert search.tokenize("Opportunities") == ["opportunity"]


def test_synonyms_expand_query(synonyms):
    q = search.build_query("Top customers by revenue", synonyms)
    assert {"partner", "client"} <= q.expansions["customer"]
    assert "partner" in q.expanded_text()


# ── Choix des tables ─────────────────────────────────────────────────────────

def test_top_customers_by_revenue(synonyms):
    p = plan("Top 5 customers by revenue", synonyms)
    assert "res_partner" in names(p)
    assert "sale_order" in names(p) or "account_move" in names(p)
    assert "account_tax" not in names(p)


def test_crm_pipeline_by_stage(synonyms):
    p = plan("Expected revenue in the CRM pipeline by stage", synonyms)
    assert names(p)[0] == "crm_lead"
    assert "crm_stage" in names(p)
    assert "utm_stage" not in names(p)[:2]


def test_unpaid_invoices_by_salesperson(synonyms):
    p = plan("Customers with the highest unpaid invoice amount, by salesperson", synonyms)
    assert {"account_move", "res_partner", "res_users"} <= set(names(p))
    # Cas observé sur la vraie base : achats en tête et tables parasites ajoutées par « référence »
    assert names(p)[0] != "purchase_order"
    assert not {"account_payment", "account_journal"} & set(names(p))


def test_key_columns_are_not_crowded_out_by_foreign_keys(synonyms):
    # Cas observé : account_move perdait amount_residual, payment_state, move_type au profit de clés étrangères
    p = plan("Customers with the highest unpaid invoice amount, by salesperson", synonyms)
    am = next(t for t in p["tables"] if t["table_name"] == "account_move")
    kept = {c["column_name"] for c in am["columns"]}
    assert {"amount_residual", "payment_state", "move_type", "partner_id", "invoice_user_id"} <= kept
    assert not kept & {"journal_id", "origin_payment_id", "tax_cash_basis_rec_id", "partner_bank_id",
                       "reversed_entry_id", "commercial_partner_id", "partner_shipping_id"}


def test_one_join_key_per_table_pair(synonyms):
    p = plan("Customers with the highest unpaid invoice amount, by salesperson", synonyms)
    pairs = [(r["from_table"], r["to_table"]) for r in p["relations"]]
    assert len(pairs) == len(set(pairs))
    assert ("account_move", "partner_id") in {(r["from_table"], r["from_column"]) for r in p["relations"]}


def test_empty_tables_are_penalised(synonyms):
    p = plan("amount per partner", synonyms)
    ranking = [r["table_name"] for r in p["ranking"]]
    assert ranking.index("account_analytic_line") > ranking.index("purchase_order_line")


def test_lookup_reason_is_reported(synonyms):
    p = plan("Expected revenue in the CRM pipeline by stage", synonyms)
    reasons = {t["table_name"]: t["reason"] for t in p["tables"]}
    assert reasons["crm_stage"] in ("search", "lookup:crm_lead.stage_id")


# ── Graphe ───────────────────────────────────────────────────────────────────

def test_join_path_adds_intermediate_table():
    _, _, _, rels = build_inputs()
    graph = search.build_graph(rels)
    assert search.join_tables(graph, ["sale_order_line", "res_partner"]) == \
        {"sale_order": "join:sale_order_line↔res_partner"}


def test_technical_foreign_keys_are_not_join_paths():
    _, _, _, rels = build_inputs()
    graph = search.build_graph(rels)
    # create_uid relie tout à res_users : il ne doit pas servir de chemin
    assert "sale_order" not in graph.get("res_users", {}) or \
        graph["res_users"]["sale_order"][1] == "user_id"
    assert "res_company" not in graph


# ── Colonnes et schéma ───────────────────────────────────────────────────────

def test_columns_are_pruned_and_commented(synonyms):
    p = plan("Top 5 customers by revenue", synonyms)
    so = next(t for t in p["tables"] if t["table_name"] == "sale_order")
    kept = {c["column_name"] for c in so["columns"]}
    assert {"id", "partner_id", "amount_total", "state"} <= kept
    assert not kept & {"create_uid", "write_uid", "create_date"}
    assert "values: draft=Quotation; sale=Sales Order" in p["schema_ddl"]
    assert "FOREIGN KEY (partner_id) REFERENCES res_partner(id)" in p["schema_ddl"]


def test_translatable_json_is_flagged(synonyms):
    p = plan("Expected revenue in the CRM pipeline by stage", synonyms)
    assert "translatable JSON: use ->>'en_US'" in p["schema_ddl"]


def test_commas_are_never_commented_out(synonyms):
    p = plan("Top 5 customers by revenue", synonyms)
    lines = p["schema_ddl"].splitlines()
    for line, nxt in zip(lines, lines[1:]):
        if line.startswith("    "):
            code = line.split("  --")[0].rstrip()
            if nxt.startswith(");"):
                assert not code.endswith(",")
            else:
                assert code.endswith(","), line


def test_token_budget_is_respected(synonyms):
    small = search.PlanParams(token_budget=400)
    p = plan("Customers with the highest unpaid invoice amount, by salesperson", synonyms, small)
    assert p["token_estimate"] <= 400 or len(p["tables"]) <= 2


# ── Valeurs citées ───────────────────────────────────────────────────────────

def test_normalize_text():
    assert search.normalize_text("Côte d'Ivoire") == "cote d ivoire"
    assert search.normalize_text("  Azure   Interior! ") == "azure interior"


def test_match_values_whole_words_and_accents():
    found = search.match_values("How many customers do we have in Morocco?", VALUES)
    assert [(m.table_name, m.value) for m in found] == [("res_country", "Morocco")]
    found = search.match_values("Invoices of customers in Cote d'Ivoire", VALUES)
    assert [m.value for m in found] == ["Côte d'Ivoire"]
    assert search.match_values("Moroccan market", VALUES) == []


def test_common_words_are_not_values():
    # « new » et « won » sont aussi des étapes CRM : trop ambigus seuls
    assert search.match_values("New customers who won deals", VALUES) == []
    assert [m.value for m in search.match_values("Leads in the Qualified stage", VALUES)] == ["Qualified"]


def test_value_brings_its_table_and_join(synonyms):
    p = plan("How many customers do we have in Morocco?", synonyms)
    assert "res_country" in names(p)
    reasons = {t["table_name"]: t["reason"] for t in p["tables"]}
    assert reasons["res_country"] in ("search", "value:res_country.name")
    assert "FOREIGN KEY (country_id) REFERENCES res_country(id)" in p["schema_ddl"]
    assert "question mentions name = 'Morocco'" in p["schema_ddl"]
