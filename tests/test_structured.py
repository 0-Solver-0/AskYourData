"""Recherche à partir d'une question découpée (sujet, mesures, filtres, regroupements), sans base ni modèle."""

import pytest

from schema_service import search, structured
from tests.test_search import VALUES, build_inputs


def run(sq_dict, table_sims, piece_sims, question=None, params=None):
    """piece_sims : {(table, colonne): {texte du morceau: similarité}} ; 0,1 par défaut."""
    sq = structured.StructuredQuestion.from_dict(sq_dict)
    _, tables, cols, rels = build_inputs()
    texts = [t for _, t in sq.pieces()]
    column_sims = {(t, c["column_name"]): [piece_sims.get((t, c["column_name"]), {}).get(x, 0.1) for x in texts]
                   for t, cs in cols.items() for c in cs}
    sims = {t: table_sims.get(t, 0.2) for t in tables}
    matches = search.match_values(question or " ".join([sq.subject, *sq.filters]), VALUES)
    return structured.plan_structured(sq, sims, column_sims, tables, cols, rels,
                                      params or structured.StructuredParams(), matches)


def names(plan):
    return {t["table_name"]: t["reason"] for t in plan["tables"]}


def columns(plan, table):
    return {c["column_name"] for t in plan["tables"] if t["table_name"] == table for c in t["columns"]}


UNPAID = {
    "subject": "customer invoices",
    "measures": ["unpaid amount"],
    "filters": ["customer invoices only", "not fully paid"],
    "group_by": ["customer", "salesperson"],
    "order": "unpaid amount desc",
}
UNPAID_SIMS = {
    ("account_move", "amount_residual"): {"unpaid amount": 0.72},
    ("account_move", "move_type"): {"customer invoices only": 0.61},
    ("account_move", "payment_state"): {"not fully paid": 0.66, "unpaid amount": 0.5},
    ("account_move", "partner_id"): {"customer": 0.58},
    ("sale_order", "partner_id"): {"customer": 0.6},          # même libellé « Customer » ailleurs
    ("account_move", "invoice_user_id"): {"salesperson": 0.7},
    ("sale_order", "user_id"): {"salesperson": 0.66},
}


def test_question_parsing():
    sq = structured.StructuredQuestion.from_dict({"subject": " customer  invoices ", "filters": "posted",
                                                  "period": "invoice date in 2025"})
    assert sq.subject == "customer invoices" and sq.filters == ["posted"]
    assert sq.pieces() == [("filter", "posted"), ("period", "invoice date in 2025")]
    with pytest.raises(ValueError):
        structured.StructuredQuestion.from_dict({"measures": ["x"]})
    with pytest.raises(ValueError):
        structured.StructuredQuestion.from_dict({"subject": "x", "filters": {"a": 1}})


def test_unpaid_invoices_by_salesperson():
    plan = run(UNPAID, {"account_move": 0.6, "sale_order": 0.5, "res_partner": 0.4}, UNPAID_SIMS)
    found = names(plan)
    assert found["account_move"] == "subject"
    assert found["res_users"] == "lookup:account_move.invoice_user_id"
    assert "res_partner" in found
    assert {"amount_residual", "payment_state", "move_type", "partner_id", "invoice_user_id"} <= \
        columns(plan, "account_move")
    assert columns(plan, "res_partner") <= {"id", "name", "country_id"}
    assert "partner_id" in columns(plan, "res_users")      # chemin vers le nom du commercial
    assert "FOREIGN KEY (partner_id) REFERENCES res_partner(id)" in plan["schema_ddl"]
    assert "account_tax" not in found and "account_journal" not in found


def test_pieces_report_their_best_columns():
    plan = run(UNPAID, {"account_move": 0.6}, UNPAID_SIMS)
    by_text = {p["text"]: p for p in plan["pieces"]}
    assert by_text["unpaid amount"]["matches"][0] == {"table": "account_move", "column": "amount_residual",
                                                      "similarity": 0.72}
    # Seconde correspondance gardée seulement si elle est presque aussi bonne que la première
    assert [m["table"] for m in by_text["customer"]["matches"]] == ["account_move", "sale_order"]
    assert [m["table"] for m in by_text["salesperson"]["matches"]] == ["account_move"]


def test_candidate_tables_limit_the_column_search():
    # sale_order hors des candidates : ses colonnes ne sont pas cherchées
    params = structured.StructuredParams(candidate_tables=1)
    plan = run(UNPAID, {"account_move": 0.6, "sale_order": 0.5}, UNPAID_SIMS, params=params)
    assert "sale_order" not in names(plan)


def test_cited_value_brings_its_table():
    sq = {"subject": "customers", "measures": ["number of customers"], "filters": ["country is Morocco"]}
    plan = run(sq, {"res_partner": 0.7}, {("res_partner", "customer_rank"): {"number of customers": 0.6}},
               question="How many customers do we have in Morocco?")
    found = names(plan)
    assert found["res_partner"] == "subject"
    assert found["res_country"] == "value:res_country.name"
    assert "FOREIGN KEY (country_id) REFERENCES res_country(id)" in plan["schema_ddl"]


def test_notes_and_long_descriptions_reach_the_schema():
    _, tables, cols, _ = build_inputs()
    table = {**tables["account_move"], "description": "x" * 400}
    col = {**next(c for c in cols["account_move"] if c["column_name"] == "payment_state"),
           "notes": "not_paid and partial mean an amount is still due"}
    ddl = search.render_table(table, [(col, "matched")], [], {"account_move"})
    assert "note: not_paid and partial mean an amount is still due" in ddl
    assert "x" * search.DESCRIPTION_MAX_CHARS in ddl and "x" * (search.DESCRIPTION_MAX_CHARS + 1) not in ddl


def test_hand_written_questions_are_valid():
    import os

    import yaml
    path = os.path.join(os.path.dirname(__file__), "..", "benchmark", "structured_questions.yaml")
    cases = yaml.safe_load(open(path, encoding="utf-8"))
    assert len(cases) == 12
    for case in cases:
        structured.StructuredQuestion.from_dict(case["structured"])
        assert case["expected"]
