"""Recherche à partir d'une question découpée (sujet, mesures, filtres, regroupements), sans base ni modèle."""

import pytest

from schema_service import search, structured
from tests.test_search import VALUES, build_inputs


def run(sq_dict, table_sims, piece_sims, question=None, params=None, table_piece_sims=None):
    """piece_sims : {(table, colonne): {texte du morceau: similarité}} ; 0,1 par défaut.
    table_piece_sims : {texte du morceau: {table: similarité}} (fiches de tables)."""
    sq = structured.StructuredQuestion.from_dict(sq_dict)
    _, tables, cols, rels = build_inputs()
    texts = [t for _, t in sq.pieces()]
    column_sims = {(t, c["column_name"]): [piece_sims.get((t, c["column_name"]), {}).get(x, 0.1) for x in texts]
                   for t, cs in cols.items() for c in cs}
    sims = {t: table_sims.get(t, 0.2) for t in tables}
    matches = search.match_values(question or " ".join([sq.subject, *sq.filters]), VALUES)
    per_piece = [(table_piece_sims or {}).get(x, {}) for x in texts]
    return structured.plan_structured(sq, sims, column_sims, tables, cols, rels,
                                      params or structured.StructuredParams(min_similarity=0.5), matches,
                                      per_piece)


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
                                                      "similarity": 0.72, "via": None}
    # Sujet d'abord : sale_order.partner_id (0,60) ne bat pas assez account_move.partner_id (0,58)
    assert [m["table"] for m in by_text["customer"]["matches"]] == ["account_move"]
    assert by_text["customer"]["subject_first"] is True
    assert [m["table"] for m in by_text["salesperson"]["matches"]] == ["account_move"]
    # Sans la règle, la seconde correspondance presque aussi bonne revient
    loose = run(UNPAID, {"account_move": 0.6}, UNPAID_SIMS, params=structured.StructuredParams(subject_margin=-1, min_similarity=0.5))
    by_text = {p["text"]: p for p in loose["pieces"]}
    assert [m["table"] for m in by_text["customer"]["matches"]] == ["sale_order", "account_move"]


def test_column_kinds():
    _, _, cols, _ = build_inputs()
    kinds = {c["column_name"]: structured.column_kind(c) for c in cols["account_move"]}
    assert kinds["amount_residual"] == "number" and kinds["invoice_date"] == "date"
    assert kinds["partner_id"] == "relation" and kinds["move_type"] == "text"
    assert structured.column_kind({"column_name": "x", "data_type": "boolean"}) == "boolean"
    assert structured.column_kind({"column_name": "x", "data_type": "integer", "odoo_type": "many2one"}) == "relation"


def test_measure_takes_a_number_and_period_a_date():
    sq = {"subject": "customer invoices", "measures": ["amount due"], "period": "due date"}
    sims = {("account_move", "invoice_date"): {"amount due": 0.79, "due date": 0.8},   # date proche du texte
            ("account_move", "amount_residual"): {"amount due": 0.77, "due date": 0.5}}
    plan = run(sq, {"account_move": 0.6}, sims)
    by_text = {p["text"]: p for p in plan["pieces"]}
    assert by_text["amount due"]["matches"][0]["column"] == "amount_residual"
    assert by_text["amount due"]["type_checked"] is True
    assert by_text["due date"]["matches"][0]["column"] == "invoice_date"
    unchecked = run(sq, {"account_move": 0.6}, sims, params=structured.StructuredParams(check_types=False))
    assert {p["text"]: p for p in unchecked["pieces"]}["amount due"]["matches"][0]["column"] == "invoice_date"


def test_another_table_wins_only_by_a_clear_margin():
    sq = {"subject": "customer invoices", "group_by": ["customer"]}
    sims = {("account_move", "partner_id"): {"customer": 0.58}, ("sale_order", "partner_id"): {"customer": 0.70}}
    plan = run(sq, {"account_move": 0.6, "sale_order": 0.5}, sims)
    assert plan["pieces"][0]["matches"][0]["table"] == "sale_order"       # 0,12 d'écart > 0,06
    assert plan["pieces"][0]["subject_first"] is False


def test_unrelated_cited_value_is_ignored():
    # « Qualified » est une étape du CRM : sans lien avec les factures, la valeur n'ajoute pas crm_stage
    sq = {"subject": "customer invoices", "measures": ["amount due"]}
    plan = run(sq, {"account_move": 0.6}, {("account_move", "amount_residual"): {"amount due": 0.7}},
               question="Amount due on invoices of Qualified deals")
    assert "crm_stage" not in names(plan)
    assert {v["table_name"] for v in plan["value_matches_ignored"]} == {"crm_stage"}


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
    for name in ("unseen_questions.yaml",):
        extra = yaml.safe_load(open(os.path.join(os.path.dirname(path), name), encoding="utf-8"))
        assert len(extra) == 10
        cases += extra
    for case in cases:
        structured.StructuredQuestion.from_dict(case["structured"])
        assert case["expected"]


def test_weak_piece_is_reported_not_found():
    sq = {"subject": "employees", "period": "hire date"}
    plan = run(sq, {"account_move": 0.6}, {("account_move", "invoice_date"): {"hire date": 0.56}},
               params=structured.StructuredParams())
    assert plan["pieces"][0]["not_found"] is True and plan["pieces"][0]["matches"] == []


def test_creation_date_is_allowed_for_a_period_only():
    _, _, cols, _ = build_inputs()
    created = {"column_name": "create_date", "data_type": "timestamp without time zone", "is_pk": False}
    assert structured._usable(created, "period") and not structured._usable(created, "group_by")


def test_group_by_naming_a_table_brings_it_with_its_path():
    # « salesperson » ressemble à la fiche de res_users : la table vient avec le chemin depuis le sujet
    sq = {"subject": "customer invoices", "group_by": ["salesperson"]}
    plan = run(sq, {"account_move": 0.6}, {}, table_piece_sims={"salesperson": {"res_users": 0.7, "crm_team": 0.5}})
    found = names(plan)
    assert found["res_users"] == "group_by-table:salesperson"
    assert plan["pieces"][0]["table_match"] == {"table": "res_users", "similarity": 0.7}
    # Trop faible : rien n'est ajouté
    plan = run(sq, {"account_move": 0.6}, {}, table_piece_sims={"salesperson": {"res_users": 0.55}})
    assert "res_users" not in names(plan)


def test_table_reached_by_a_group_by_gets_its_name():
    # res_users n'a pas de nom : elle suit partner_id vers res_partner, même sans clé suivie
    sq = {"subject": "customer invoices", "group_by": ["salesperson"]}
    plan = run(sq, {"account_move": 0.6}, {}, table_piece_sims={"salesperson": {"res_users": 0.7}})
    found = names(plan)
    assert found["res_partner"] == "name:res_users.partner_id"
    assert "FOREIGN KEY (partner_id) REFERENCES res_partner(id)" in plan["schema_ddl"]


def test_always_included_columns_follow_their_table():
    # move_type marqué par l'enrichissement : envoyé même si aucun morceau ne le demande
    sq = {"subject": "customer invoices", "measures": ["amount due"]}
    sims = {("account_move", "amount_residual"): {"amount due": 0.7}}
    plan = run(sq, {"account_move": 0.6}, sims)
    assert "move_type" not in columns(plan, "account_move")
    q = structured.StructuredQuestion.from_dict(sq)
    _, tables, cols, rels = build_inputs()
    cols = {t: [{**c, "always_include": c["column_name"] == "move_type"} for c in cs] for t, cs in cols.items()}
    column_sims = {(t, c["column_name"]): [sims.get((t, c["column_name"]), {}).get("amount due", 0.1)]
                   for t, cs in cols.items() for c in cs}
    plan = structured.plan_structured(q, {t: 0.6 if t == "account_move" else 0.2 for t in tables}, column_sims,
                                      tables, cols, rels, structured.StructuredParams())
    reasons = {c["column_name"]: c["reason"] for t in plan["tables"] if t["table_name"] == "account_move"
               for c in t["columns"]}
    assert reasons["move_type"] == "always"


def test_resolved_entity_does_not_bring_a_neighbour_table():
    # « salesperson » trouve account_move.invoice_user_id (clé du sujet) : crm_team, voisine
    # par le texte, n'est pas ajoutée ; res_users arrive par la clé
    sq = {"subject": "customer invoices", "group_by": ["salesperson"]}
    plan = run(sq, {"account_move": 0.6}, {("account_move", "invoice_user_id"): {"salesperson": 0.75}},
               table_piece_sims={"salesperson": {"crm_team": 0.7}})
    found = names(plan)
    assert "crm_team" not in found
    assert found["res_users"] == "lookup:account_move.invoice_user_id"


def test_name_hop_prefers_the_required_key():
    # res_users → partner_id (obligatoire) plutôt que sale_team_id vers crm_team déjà retenue
    sq = {"subject": "customer invoices", "group_by": ["salesperson", "sales team"]}
    q = structured.StructuredQuestion.from_dict(sq)
    _, tables, cols, rels = build_inputs()
    cols = {**cols, "res_users": [{**c, "is_nullable": c["column_name"] != "partner_id"} for c in cols["res_users"]]
            + [{"column_name": "sale_team_id", "data_type": "integer", "is_fk": True, "is_pk": False,
                "is_nullable": True}]}
    rels = rels + [{"from_table": "res_users", "from_column": "sale_team_id", "to_table": "crm_team",
                    "to_column": "id"}]
    texts = [t for _, t in q.pieces()]
    column_sims = {(t, c["column_name"]): [0.1 for _ in texts] for t, cs in cols.items() for c in cs}
    per_piece = [{"res_users": 0.7}, {"crm_team": 0.7}]
    plan = structured.plan_structured(q, {t: 0.6 if t == "account_move" else 0.2 for t in tables}, column_sims,
                                      tables, cols, rels, structured.StructuredParams(), None, per_piece)
    found = names(plan)
    assert found["res_partner"] == "name:res_users.partner_id"


# ── Recherche complémentaire (mots manquants signalés par le modèle SQL) ─────

def test_neighbor_tables_follow_keys_both_ways():
    _, tables, _, rels = build_inputs()
    around = structured.neighbor_tables(["sale_order"], rels, tables)
    assert around[0] == "sale_order"
    assert {"res_partner", "crm_team", "sale_order_line"} <= set(around)   # sortantes et entrantes
    assert "res_company" not in around                                     # clé technique ignorée


def test_missing_word_found_as_a_column_then_added_to_the_plan():
    _, tables, cols, rels = build_inputs()
    params = structured.StructuredParams()
    extra_cols, extra_tabs, report = structured.find_missing(
        ["balance due of the invoice"], ["account_move"],
        {("account_move", "amount_residual"): [0.72], ("account_move", "amount_total"): [0.65]},
        [{}], tables, cols, rels, params)
    assert extra_cols == {("account_move", "amount_residual"): "balance due of the invoice"} and not extra_tabs
    assert report[0]["found"] == "column" and report[0]["column"] == "amount_residual"

    sq = structured.StructuredQuestion.from_dict({"subject": "customer invoices"})
    base = structured.plan_structured(sq, {"account_move": 0.6}, {}, tables, cols, rels, params)
    assert "amount_residual" not in columns(base, "account_move")
    plan = structured.plan_structured(sq, {"account_move": 0.6}, {}, tables, cols, rels, params,
                                      extra_columns=extra_cols)
    assert "amount_residual" in columns(plan, "account_move")
    assert set(names(base)) <= set(names(plan))                            # rien n'est retiré


def test_missing_word_found_as_a_related_table():
    _, tables, cols, rels = build_inputs()
    params = structured.StructuredParams()
    extra_cols, extra_tabs, report = structured.find_missing(
        ["country of the customer"], ["account_move"], {("account_move", "partner_id"): [0.5]},
        [{"res_country": 0.7, "crm_stage": 0.65}], tables, cols, rels, params)
    assert not extra_cols and extra_tabs == {"res_country": "country of the customer"}
    assert report[0]["found"] == "table"
    sq = structured.StructuredQuestion.from_dict({"subject": "customer invoices"})
    plan = structured.plan_structured(sq, {"account_move": 0.6}, {}, tables, cols, rels, params,
                                      extra_tables=extra_tabs)
    found = names(plan)
    assert found["res_country"] == "missing-table:country of the customer"
    assert "res_partner" in found                                          # chemin account_move → res_partner


def test_missing_word_not_found_is_reported():
    _, tables, cols, rels = build_inputs()
    extra_cols, extra_tabs, report = structured.find_missing(
        ["carrier"], ["account_move"], {("account_move", "partner_id"): [0.55]}, [{"crm_stage": 0.5}],
        tables, cols, rels, structured.StructuredParams())
    assert not extra_cols and not extra_tabs
    assert report == [{"text": "carrier", "found": None, "table": "account_move", "column": "partner_id",
                       "similarity": 0.55}]


def test_missing_word_keeps_the_best_column_of_the_selected_tables_too():
    # « manager » : hr_department.manager_id (voisine) est meilleure, mais la colonne du sujet compte aussi
    _, tables, cols, rels = build_inputs()
    extra_cols, _, report = structured.find_missing(
        ["payment amount"], ["account_move"],
        {("account_payment", "amount"): [0.82], ("account_move", "amount_total"): [0.7]},
        [{}], tables, cols, rels, structured.StructuredParams())
    assert set(extra_cols) == {("account_move", "amount_total"), ("account_payment", "amount")}
    assert (report[0]["table"], report[0]["column"]) == ("account_move", "amount_total")
    assert report[0]["also"] == [{"table": "account_payment", "column": "amount", "similarity": 0.82}]


def test_per_company_json_columns_are_flagged():
    comment = search.column_comment({"column_name": "standard_price", "data_type": "jsonb", "odoo_type": "float",
                                     "odoo_label": "Cost"})
    assert "per-company JSON" in comment and "translatable" not in comment
    assert "translatable JSON" in search.column_comment({"column_name": "name", "data_type": "jsonb",
                                                         "odoo_type": "char"})


def test_missing_word_prefers_the_subject_table_column():
    # « manager » : hr_department (retenue, non sujet) a la meilleure colonne, mais celle du sujet est gardée
    _, tables, cols, rels = build_inputs()
    extra_cols, _, report = structured.find_missing(
        ["vendor of the bill"], ["account_move", "purchase_order"],
        {("purchase_order", "partner_id"): [0.8], ("account_move", "partner_id"): [0.68]},
        [{}], tables, cols, rels, structured.StructuredParams())
    assert ("account_move", "partner_id") in extra_cols and ("purchase_order", "partner_id") in extra_cols
    assert (report[0]["table"], report[0]["column"]) == ("account_move", "partner_id")


def test_missing_word_kind_from_its_words():
    assert structured.expected_kind("sale date of the sales order") == "date"
    assert structured.expected_kind("quantity sold on sales order lines") == "number"
    assert structured.expected_kind("unit selling price on sales order lines") == "number"
    assert structured.expected_kind("number of days between the due date and today") is None   # les deux
    assert structured.expected_kind("vendor of the bill") is None


def test_missing_word_only_gets_a_column_of_the_right_kind():
    # cas réels : « sale date » → sale_order.origin (texte) ; « quantity sold » → sale_order.state (statut)
    _, tables, cols, rels = build_inputs()
    params = structured.StructuredParams()
    sims = {("account_move", "move_type"): [0.78, 0.8], ("account_move", "payment_state"): [0.74, 0.79],
            ("account_move", "invoice_date"): [0.7, 0.4], ("account_move", "amount_residual"): [0.5, 0.71]}
    extra_cols, _, report = structured.find_missing(
        ["invoice date of the bill", "unpaid amount of the bill"], ["account_move"], sims, [{}, {}],
        tables, cols, rels, params)
    assert set(extra_cols) == {("account_move", "invoice_date"), ("account_move", "amount_residual")}
    assert [r["column"] for r in report] == ["invoice_date", "amount_residual"]


def test_missing_word_without_a_column_of_the_right_kind_looks_for_a_table():
    _, tables, cols, rels = build_inputs()
    extra_cols, _, report = structured.find_missing(
        ["scheduled date of the order"], ["account_move"], {("account_move", "move_type"): [0.8]}, [{}],
        tables, cols, rels, structured.StructuredParams())
    assert not extra_cols and report[0]["found"] is None


# ── Structure : lignes du document, chemins orientés, jointures, manques ─────

def sales_world():
    """sale_order + lignes + produit → modèle → catégorie, et deux tables « parasites » qui ont aussi
    un produit et une catégorie (règle de rangement, point de commande)."""
    from tests.test_search import col
    t = lambda name: {"table_name": name, "odoo_label": name}  # noqa: E731
    cols = {
        "sale_order": [col("id", pk=True), col("name", "character varying", "Order Reference"),
                       col("user_id", label="Salesperson", fk=True), col("amount_total", "numeric", "Total",
                                                                         odoo_type="monetary"),
                       col("date_order", "timestamp without time zone", "Order Date", odoo_type="datetime")],
        "sale_order_line": [col("id", pk=True), col("order_id", label="Order", fk=True),
                            col("product_id", label="Product", fk=True),
                            col("price_subtotal", "numeric", "Subtotal", odoo_type="monetary")],
        "product_product": [col("id", pk=True), col("product_tmpl_id", label="Template", fk=True)],
        "product_template": [col("id", pk=True), col("name", "jsonb", "Name", odoo_type="char"),
                             col("categ_id", label="Product Category", fk=True)],
        "product_category": [col("id", pk=True), col("name", "character varying", "Name")],
        "stock_putaway_rule": [col("id", pk=True), col("product_id", label="Product", fk=True),
                               col("category_id", label="Product Category", fk=True)],
        "stock_warehouse_orderpoint": [col("id", pk=True), col("product_id", label="Product", fk=True),
                                       col("product_category_id", label="Product Category", fk=True)],
        "res_users": [col("id", pk=True), col("login", "character varying", "Login")],
    }
    rels = [{"from_table": a, "from_column": c, "to_table": b, "to_column": "id"} for a, c, b in (
        ("sale_order", "user_id", "res_users"), ("sale_order_line", "order_id", "sale_order"),
        ("sale_order_line", "product_id", "product_product"), ("product_product", "product_tmpl_id", "product_template"),
        ("product_template", "categ_id", "product_category"),
        ("stock_putaway_rule", "product_id", "product_product"), ("stock_putaway_rule", "category_id", "product_category"),
        ("stock_warehouse_orderpoint", "product_id", "product_product"),
        ("stock_warehouse_orderpoint", "product_category_id", "product_category"))]
    return {n: t(n) for n in cols}, cols, rels


def test_line_table_is_found_by_name_and_key():
    tables, _, rels = sales_world()
    assert structured.line_table("sale_order", rels, tables) == "sale_order_line"
    assert structured.line_table("product_template", rels, tables) is None


def test_forward_path_follows_the_keys_in_their_direction():
    _, _, rels = sales_world()
    graph, forward = search.build_graph(rels), structured.build_forward_graph(rels)
    path = structured._path_from_selected(graph, ["sale_order", "sale_order_line"], "product_category", 4, forward)
    assert path == ["sale_order_line", "product_product", "product_template", "product_category"]


def test_revenue_by_category_brings_the_lines_and_only_the_real_path():
    # Cas réel : 14 tables envoyées dont stock_putaway_rule, stock_warehouse_orderpoint… ; mesure absente
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders", "measures": ["revenue"],
                                                  "group_by": ["product category"], "period": "order date"})
    texts = [t for _, t in sq.pieces()]
    sims = {(t, c["column_name"]): [0.1] * len(texts) for t, cs in cols.items() for c in cs}
    sims[("sale_order_line", "price_subtotal")] = [0.75 if x == "revenue" else 0.1 for x in texts]
    sims[("sale_order", "amount_total")] = [0.7 if x == "revenue" else 0.1 for x in texts]
    sims[("sale_order", "date_order")] = [0.8 if x == "order date" else 0.1 for x in texts]
    table_sims = {"sale_order": 0.7, "sale_order_line": 0.6, "stock_putaway_rule": 0.3}
    per_piece = [{"product_category": 0.8} if x == "product category" else {} for x in texts]
    plan = structured.plan_structured(sq, table_sims, sims, tables, cols, rels, structured.StructuredParams(),
                                      None, per_piece)
    found = names(plan)
    assert found["sale_order_line"] == "lines:sale_order"
    assert {"product_product", "product_template", "product_category"} <= set(found)
    assert "stock_putaway_rule" not in found and "stock_warehouse_orderpoint" not in found
    assert "price_subtotal" in columns(plan, "sale_order_line")


def test_no_lines_for_a_plain_list():
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "quotations", "filters": ["status"]})
    sims = {(t, c["column_name"]): [0.1] for t, cs in cols.items() for c in cs}
    plan = structured.plan_structured(sq, {"sale_order": 0.7}, sims, tables, cols, rels, structured.StructuredParams())
    assert "sale_order_line" not in names(plan)


def test_missing_word_never_returns_a_column_already_sent():
    # Cas réel : « received status » → sale_order.state, déjà dans le schéma
    _, tables, cols, rels = build_inputs()
    sims = {("account_move", "payment_state"): [0.8], ("account_move", "amount_residual"): [0.5]}
    extra, _, report = structured.find_missing(["received status of the bill"], ["account_move"], sims, [{}],
                                               tables, cols, rels, structured.StructuredParams(),
                                               present={("account_move", "payment_state")})
    assert ("account_move", "payment_state") not in extra and report[0]["found"] is None


def test_missing_records_prefer_the_table_over_a_column():
    # Cas réel : « supplier invoices and their amounts » → res_partner.supplier_rank au lieu de account_move
    _, tables, cols, rels = build_inputs()
    extra_cols, extra_tabs, report = structured.find_missing(
        ["supplier invoices and their amounts"], ["purchase_order", "res_partner"],
        {("res_partner", "customer_rank"): [0.62]}, [{"account_move": 0.71}], tables, cols, rels,
        structured.StructuredParams())
    assert extra_tabs == {"account_move": "supplier invoices and their amounts"} and not extra_cols
    assert report[0]["found"] == "table"


def test_group_by_key_to_another_table_does_not_resolve_the_entity():
    # Cas réel : « product category » → sale_order_line.product_id (0,653) ; product_category (0,615)
    # n'était pas ajoutée (règle « entité déjà résolue ») → categ_id demandé, relance
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders", "measures": ["revenue"],
                                                  "group_by": ["product category"]})
    texts = [t for _, t in sq.pieces()]
    sims = {(t, c["column_name"]): [0.1] * len(texts) for t, cs in cols.items() for c in cs}
    sims[("sale_order_line", "price_subtotal")] = [0.7 if x == "revenue" else 0.1 for x in texts]
    sims[("sale_order_line", "product_id")] = [0.653 if x == "product category" else 0.1 for x in texts]
    per_piece = [{"product_category": 0.615} if x == "product category" else {} for x in texts]
    plan = structured.plan_structured(sq, {"sale_order": 0.6}, sims, tables, cols, rels,
                                      structured.StructuredParams(), None, per_piece)
    found = names(plan)
    assert found["product_category"] == "group_by-table:product category"
    assert "categ_id" in columns(plan, "product_template")
    assert "stock_putaway_rule" not in found


def test_group_by_key_to_the_found_table_resolves_the_entity():
    # « salesperson » → sale_order.user_id → res_users : la table trouvée est déjà la cible de la clé
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders", "group_by": ["salesperson"]})
    sims = {(t, c["column_name"]): [0.1] for t, cs in cols.items() for c in cs}
    sims[("sale_order", "user_id")] = [0.8]
    plan = structured.plan_structured(sq, {"sale_order": 0.6}, sims, tables, cols, rels,
                                      structured.StructuredParams(), None, [{"res_users": 0.7}])
    assert names(plan)["res_users"].startswith("lookup:")


def test_header_comes_before_its_lines_as_subject():
    # Calibrage Contriever : sale_order_line 0,649 devant sale_order 0,615 (4 questions sur 22)
    tables, _, rels = sales_world()
    sims = {"sale_order_line": 0.649, "sale_order": 0.615, "product_template": 0.5}
    assert structured.order_subjects(sims, tables, rels)[:2] == ["sale_order", "sale_order_line"]
    assert structured.order_subjects({"product_template": 0.7, "sale_order": 0.6}, tables, rels)[0] == \
        "product_template"


def test_forced_subject_goes_first_and_a_forced_line_table_means_its_header():
    tables, _, rels = sales_world()
    sims = {"sale_order": 0.7, "product_template": 0.6, "sale_order_line": 0.5}
    assert structured.order_subjects(sims, tables, rels, subject="product_template")[0] == "product_template"
    assert structured.order_subjects(sims, tables, rels, subject="sale_order_line")[0] == "sale_order"
    assert structured.order_subjects(sims, tables, rels, subject="unknown_table")[0] == "sale_order"


def test_plan_reports_how_the_subject_was_chosen():
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders", "measures": ["revenue"]})
    sims = {(t, c["column_name"]): [0.1] for t, cs in cols.items() for c in cs}
    plan = structured.plan_structured(sq, {"sale_order_line": 0.65, "sale_order": 0.61}, sims, tables, cols, rels,
                                      structured.StructuredParams())
    assert plan["subject_table"] == "sale_order" and plan["subject_by"] == "header"
    assert plan["subject_ranking"][0]["table_name"] == "sale_order_line"          # classement brut affiché
    forced = structured.plan_structured(sq, {"sale_order": 0.7}, sims, tables, cols, rels,
                                        structured.StructuredParams(), subject="res_users")
    assert forced["subject_table"] == "res_users" and forced["subject_by"] == "forced"


def test_entities_are_parsed_and_ignored_when_they_name_the_subject():
    sq = structured.StructuredQuestion.from_dict({
        "subject": "vendor bills", "group_by": ["Vendor"], "filters": ["country"],
        "entities": {"vendor": "vendor", "Country": " customer ", "due date": "vendor bill", "x": ""}})
    assert sq.entity_of("Vendor") == "vendor" and sq.entity_of("country") == "customer"
    assert sq.entity_of("due date") is None                    # même chose que le sujet (singulier/pluriel)
    assert sq.entity_of("x") is None
    listed = structured.StructuredQuestion.from_dict({"subject": "sales orders",
                                                      "entities": [{"text": "category", "entity": "product"}, "bad"]})
    assert listed.entities == {"category": "product"}


def _entity_plan(group_by, entity, table, sims_for):
    tables, cols, rels = sales_world()
    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders", "measures": ["revenue"],
                                                  "group_by": [group_by], "entities": {group_by: entity}})
    texts = [t for _, t in sq.pieces()]
    sims = {(t, c["column_name"]): [0.1] * len(texts) for t, cs in cols.items() for c in cs}
    sims[("sale_order_line", "price_subtotal")] = [0.75 if x == "revenue" else 0.1 for x in texts]
    for key, value in sims_for.items():
        sims[key] = [value if x == group_by else 0.1 for x in texts]
    table_sims = {"sale_order": 0.7, "sale_order_line": 0.6, "stock_putaway_rule": 0.3}
    ents = [{"entity": entity, "table": table, "similarity": 0.8} if x == group_by else None for x in texts]
    return structured.plan_structured(sq, table_sims, sims, tables, cols, rels, structured.StructuredParams(),
                                      None, None, piece_entities=ents)


def test_entity_attribute_is_searched_in_the_entity_table_first():
    # « category » d'un produit : product_template.categ_id, pas la catégorie d'une règle de rangement
    plan = _entity_plan("category", "product", "product_template",
                        {("product_template", "categ_id"): 0.66, ("stock_putaway_rule", "category_id"): 0.8})
    piece = next(p for p in plan["pieces"] if p["text"] == "category")
    assert piece["entity"]["rule"] == "attribute"
    assert [(m["table"], m["column"]) for m in piece["matches"]] == [("product_template", "categ_id")]
    found = names(plan)
    assert {"product_product", "product_template", "product_category"} <= set(found)
    assert "stock_putaway_rule" not in found


def test_entity_named_by_the_piece_takes_the_subject_key():
    # « salesperson » = l'entité elle-même : la clé sale_order.user_id vers sa table
    plan = _entity_plan("salesperson", "salesperson", "res_users", {("stock_putaway_rule", "product_id"): 0.9})
    piece = next(p for p in plan["pieces"] if p["text"] == "salesperson")
    assert piece["entity"]["rule"] == "fk" and not piece["not_found"]
    assert [(m["table"], m["column"]) for m in piece["matches"]] == [("sale_order", "user_id")]
    assert "res_users" in names(plan) and "stock_putaway_rule" not in names(plan)


def test_entity_without_a_key_from_the_subject_brings_its_table_and_path():
    plan = _entity_plan("category", "category", "product_category", {("stock_putaway_rule", "category_id"): 0.9})
    piece = next(p for p in plan["pieces"] if p["text"] == "category")
    assert piece["entity"]["rule"] == "table" and piece["matches"] == []
    found = names(plan)
    assert found["product_category"] == "entity:category"
    assert {"product_product", "product_template"} <= set(found) and "stock_putaway_rule" not in found


def test_entity_table_is_the_candidate_the_subject_references():
    # « salesperson » : la première fiche trouvée est une autre table ; le sujet référence res_users
    tables, cols, rels = sales_world()
    fk = {(r["from_table"], r["from_column"]): r["to_table"] for r in rels}
    ent = {"entity": "salesperson", "candidates": [{"table": "stock_putaway_rule", "similarity": 0.8, "via": "x"},
                                                   {"table": "res_users", "similarity": 0.7, "via": "salesperson"}]}
    chosen = structured._choose_entity_table(ent, {"sale_order"}, fk, tables)
    assert chosen["table"] == "res_users" and chosen["via"] == "salesperson"
    # Aucune candidate référencée : la première
    assert structured._choose_entity_table(ent, {"product_category"}, fk, tables)["table"] == "stock_putaway_rule"
    assert structured._choose_entity_table(None, {"sale_order"}, fk, tables) is None
