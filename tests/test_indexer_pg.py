"""
Test d'intégration de l'indexation sur une mini base « Odoo » simulée.

Nécessite un PostgreSQL avec l'extension pgvector. Ignoré si TEST_PG_DSN n'est pas défini.
Exemple : TEST_PG_DSN="host=localhost port=55432 user=postgres" pytest tests/test_indexer_pg.py
L'embedder est remplacé par une fonction déterministe : aucun modèle n'est chargé.
"""

import hashlib
import os
import uuid

import pytest

pytest.importorskip("psycopg2")
pytest.importorskip("pgvector")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from schema_service.filters import FilterConfig, load_filter_config  # noqa: E402
from schema_service.indexer import index_account, index_report, reindex_table_embeddings  # noqa: E402

DSN = os.getenv("TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN non défini (PostgreSQL + pgvector requis)")

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCHEMA_SQL = os.path.join(ROOT, "schema_service", "schema.sql")
CONFIG_PATH = os.path.join(ROOT, "config", "index_filters.yaml")

FAKE_ODOO = """
CREATE TABLE ir_model (id serial PRIMARY KEY, model varchar, name jsonb, transient boolean);
CREATE TABLE ir_model_fields (id serial PRIMARY KEY, model varchar, name varchar, ttype varchar,
    relation varchar, field_description jsonb, help jsonb, store boolean);
CREATE TABLE ir_model_fields_selection (id serial PRIMARY KEY, field_id int, value varchar,
    name jsonb, sequence int);
CREATE TABLE ir_ui_view (id serial PRIMARY KEY, arch text);
CREATE TABLE mail_message (id serial PRIMARY KEY, body text);
CREATE TABLE res_country (id serial PRIMARY KEY, name jsonb, code varchar);
CREATE TABLE res_partner (id serial PRIMARY KEY, name varchar, email varchar, signup_token varchar,
    country_id int REFERENCES res_country(id));
CREATE TABLE res_users (id serial PRIMARY KEY, login varchar, password varchar,
    partner_id int REFERENCES res_partner(id));
CREATE TABLE res_users_log (id serial PRIMARY KEY, create_uid int REFERENCES res_users(id));
CREATE TABLE sale_order (id serial PRIMARY KEY, name varchar, state varchar, amount_total numeric,
    partner_id int REFERENCES res_partner(id), user_id int REFERENCES res_users(id),
    create_uid int REFERENCES res_users(id));
CREATE TABLE sale_order_line (id serial PRIMARY KEY, order_id int REFERENCES sale_order(id),
    product_uom_qty numeric, price_subtotal numeric);
CREATE TABLE sale_advance_payment_inv (id serial PRIMARY KEY, advance_payment_method varchar);
CREATE TABLE account_tax_sale_order_line_rel (sale_order_line_id int, account_tax_id int);
CREATE TABLE custom_table_without_model (id serial PRIMARY KEY, x int);
CREATE TABLE ir_model_data (id serial PRIMARY KEY, module varchar, name varchar, model varchar, res_id int);

INSERT INTO ir_model (model, name, transient) VALUES
  ('res.partner', '{"en_US": "Contact"}', false),
  ('res.country', '{"en_US": "Country"}', false),
  ('res.users', '{"en_US": "User"}', false),
  ('res.users.log', '{"en_US": "Users Log"}', false),
  ('sale.order', '{"en_US": "Sales Order"}', false),
  ('sale.order.line', '{"en_US": "Sales Order Line"}', false),
  ('sale.advance.payment.inv', '{"en_US": "Sales Advance Payment Invoice"}', true),
  ('mail.message', '{"en_US": "Message"}', false),
  ('ir.ui.view', '{"en_US": "View"}', false);

INSERT INTO ir_model_data (module, name, model, res_id) VALUES
  ('base', 'model_res_partner', 'ir.model', 1), ('base', 'model_res_country', 'ir.model', 2),
  ('base', 'model_res_users', 'ir.model', 3), ('sale', 'model_sale_order', 'ir.model', 5),
  ('sale', 'model_sale_order_line', 'ir.model', 6),
  ('sale_crm', 'model_sale_order', 'ir.model', 5),      -- extension : ne change pas le module d'origine
  ('base', 'partner_admin', 'res.partner', 1);

INSERT INTO ir_model_fields (model, name, ttype, relation, field_description, help, store) VALUES
  ('sale.order', 'state', 'selection', NULL, '{"en_US": "Status"}', NULL, true),
  ('sale.order', 'amount_total', 'monetary', NULL, '{"en_US": "Total"}', '{"en_US": "Total incl. taxes"}', true),
  ('sale.order', 'partner_id', 'many2one', 'res.partner', '{"en_US": "Customer"}', NULL, true),
  ('sale.order', 'user_id', 'many2one', 'res.users', '{"en_US": "Salesperson"}', NULL, true),
  ('sale.order', 'note_computed', 'char', NULL, '{"en_US": "Not stored"}', NULL, false),
  ('sale.order.line', 'price_subtotal', 'monetary', NULL, '{"en_US": "Subtotal"}', NULL, true),
  ('res.partner', 'name', 'char', NULL, '{"en_US": "Name"}', NULL, true);

INSERT INTO ir_model_fields_selection (field_id, value, name, sequence)
SELECT id, 'draft', '{"en_US": "Quotation"}'::jsonb, 1 FROM ir_model_fields WHERE model='sale.order' AND name='state'
UNION ALL
SELECT id, 'sale', '{"en_US": "Sales Order"}'::jsonb, 2 FROM ir_model_fields WHERE model='sale.order' AND name='state';

INSERT INTO res_country (name, code) VALUES ('{"en_US": "Morocco"}', 'MA'), ('{"en_US": "France"}', 'FR');
INSERT INTO res_partner (name, country_id) VALUES ('Azure Interior', 1), ('Deco Addict', 2);
INSERT INTO res_users (login, partner_id) VALUES ('admin', 1);
INSERT INTO sale_order (name, state, amount_total, partner_id, user_id) VALUES ('S001', 'sale', 100, 2, 1);
"""


def fake_embed(texts):
    """Vecteurs déterministes de 384 dimensions (pas de modèle)."""
    out = []
    for t in texts:
        digest = hashlib.sha256(t.encode()).digest()
        out.append([(digest[i % 32] - 128) / 128.0 for i in range(384)])
    return out


@pytest.fixture(scope="module")
def dbs():
    admin = psycopg2.connect(DSN, dbname="postgres")
    admin.autocommit = True
    suffix = uuid.uuid4().hex[:8]
    src_name, nl_name = f"t_odoo_{suffix}", f"t_nl2sql_{suffix}"
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {src_name}")
        cur.execute(f"CREATE DATABASE {nl_name}")

    src = psycopg2.connect(DSN, dbname=src_name)
    with src.cursor() as cur:
        cur.execute(FAKE_ODOO)
    src.commit()

    nl = psycopg2.connect(DSN, dbname=nl_name)
    with nl.cursor() as cur, open(SCHEMA_SQL, encoding="utf-8") as fh:
        cur.execute(fh.read())
    nl.commit()
    # Le schéma doit pouvoir être rejoué sans erreur (mise à niveau idempotente)
    with nl.cursor() as cur, open(SCHEMA_SQL, encoding="utf-8") as fh:
        cur.execute(fh.read())
    nl.commit()
    register_vector(nl)

    with nl.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM nl2sql_accounts")
        accounts = cur.fetchall()
    assert len(accounts) == 1, "le compte de démo ne doit être créé qu'une fois"
    account = dict(accounts[0])

    yield src, nl, account

    src.close()
    nl.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {src_name}")
        cur.execute(f"DROP DATABASE {nl_name}")
    admin.close()


def _tables(nl, account_id):
    with nl.cursor() as cur:
        cur.execute("SELECT table_name FROM nl2sql_tables WHERE account_id=%s ORDER BY 1", (account_id,))
        return [r[0] for r in cur.fetchall()]


def test_dry_run_writes_nothing(dbs):
    src, nl, account = dbs
    report = index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH), dry_run=True)
    assert sorted(report["retained_tables"]) == ["res_country", "res_partner", "res_users", "sale_order", "sale_order_line"]
    assert report["has_odoo_metadata"] is True
    assert _tables(nl, account["id"]) == []


def test_full_index(dbs):
    src, nl, account = dbs
    report = index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    assert report["indexed"] == 5 and report["failed"] == []

    reasons = {e["table_name"]: e["reason"] for e in index_report(nl, account["id"])["excluded_tables"]}
    assert reasons["sale_advance_payment_inv"] == "transient_model"
    assert reasons["ir_ui_view"] == "prefix:ir_"
    assert reasons["mail_message"] == "prefix:mail_"
    assert reasons["res_users_log"] == "prefix:res_users_"
    assert reasons["account_tax_sale_order_line_rel"] == "suffix:_rel"
    assert reasons["custom_table_without_model"] == "no_odoo_model"

    with nl.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT t.table_name, t.odoo_model, t.odoo_label, t.row_count, t.embedding IS NOT NULL AS emb
            FROM nl2sql_tables t WHERE t.table_name = 'sale_order'
        """)
        so = cur.fetchone()
        assert (so["odoo_model"], so["odoo_label"], so["row_count"], so["emb"]) == \
            ("sale.order", "Sales Order", 1, True)

        cur.execute("""
            SELECT c.column_name, c.odoo_label, c.odoo_help, c.selection_values, c.relation_model,
                   c.is_fk, c.embedding IS NOT NULL AS emb
            FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
            WHERE t.table_name = 'sale_order'
        """)
        cols = {r["column_name"]: r for r in cur.fetchall()}
        assert cols["state"]["selection_values"] == "draft=Quotation; sale=Sales Order"
        assert cols["amount_total"]["odoo_help"] == "Total incl. taxes"
        assert cols["user_id"]["relation_model"] == "res.users" and cols["user_id"]["is_fk"]
        assert all(c["emb"] for c in cols.values())

        # Colonnes sensibles jamais indexées
        cur.execute("""
            SELECT c.column_name FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
            WHERE c.column_name IN ('password', 'signup_token')
        """)
        assert cur.fetchall() == []

        # Relations uniquement entre tables retenues
        cur.execute("SELECT from_table, from_column, to_table FROM nl2sql_relations")
        rels = {(r["from_table"], r["from_column"], r["to_table"]) for r in cur.fetchall()}
        assert ("sale_order", "user_id", "res_users") in rels
        assert ("sale_order_line", "order_id", "sale_order") in rels
        assert not any(r[0] == "res_users_log" for r in rels)


def test_reindex_preserves_manual_and_enriched_fields(dbs):
    src, nl, account = dbs
    with nl.cursor() as cur:
        cur.execute("""
            UPDATE nl2sql_tables SET description = 'Customer orders', label = 'Orders', synonyms = 'orders'
            WHERE table_name = 'sale_order'
        """)
        cur.execute("""
            UPDATE nl2sql_columns SET label = 'Order total' WHERE column_name = 'amount_total'
        """)
    nl.commit()

    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))

    with nl.cursor() as cur:
        cur.execute("SELECT description, label, synonyms FROM nl2sql_tables WHERE table_name = 'sale_order'")
        assert cur.fetchone() == ("Customer orders", "Orders", "orders")
        cur.execute("SELECT label FROM nl2sql_columns WHERE column_name = 'amount_total'")
        assert cur.fetchone() == ("Order total",)


def test_stale_tables_and_relations_are_removed(dbs):
    src, nl, account = dbs
    cfg = load_filter_config(CONFIG_PATH)
    cfg = FilterConfig(**{**cfg.__dict__, "exclude_tables": cfg.exclude_tables + ["sale_order_line"]})
    index_account(nl, src, account, fake_embed, cfg)

    assert "sale_order_line" not in _tables(nl, account["id"])
    with nl.cursor() as cur:
        cur.execute("SELECT count(*) FROM nl2sql_relations WHERE from_table = 'sale_order_line'")
        assert cur.fetchone()[0] == 0


def test_reindex_table_embeddings(dbs):
    src, nl, account = dbs
    with nl.cursor() as cur:
        cur.execute("UPDATE nl2sql_columns SET embedding = NULL")
        cur.execute("SELECT id FROM nl2sql_tables WHERE table_name = 'res_partner'")
        table_id = cur.fetchone()[0]
    nl.commit()
    reindex_table_embeddings(nl, table_id, fake_embed)
    with nl.cursor() as cur:
        cur.execute("SELECT bool_and(embedding IS NOT NULL) FROM nl2sql_columns WHERE table_id = %s", (table_id,))
        assert cur.fetchone()[0] is True


def test_default_account_follows_configuration(dbs):
    from schema_service.accounts import sync_default_account

    _, nl, account = dbs
    params = {"pg_host": "odoo_host", "pg_port": 5432, "pg_db": "Meta_Global_AI_Hackathon",
              "pg_user": "odoo", "pg_schema": "public"}
    row = sync_default_account(nl, params)
    assert row["id"] == 1 and row["pg_db"] == "Meta_Global_AI_Hackathon"

    # Idempotent, un seul compte, et la séquence reste utilisable pour un nouveau compte
    sync_default_account(nl, params)
    with nl.cursor() as cur:
        cur.execute("SELECT count(*) FROM nl2sql_accounts")
        assert cur.fetchone()[0] == 1
        cur.execute("INSERT INTO nl2sql_accounts (name) VALUES ('other') RETURNING id")
        assert cur.fetchone()[0] == 2
    nl.rollback()

    # Remet le compte de test dans son état d'origine
    sync_default_account(nl, {k: account[k] for k in params})


def test_reference_values_are_indexed(dbs):
    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("SELECT table_name, value, normalized FROM nl2sql_values ORDER BY 1, 2")
        rows = cur.fetchall()
    assert ("res_country", "Morocco", "morocco") in rows          # JSONB lu en anglais
    assert ("res_partner", "Azure Interior", "azure interior") in rows


def test_vector_space_follows_the_embedder(dbs):
    from schema_service.vector_space import current_dimension, ensure_vector_space, get_meta

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    assert current_dimension(nl) == 384

    # Index existant sans modèle enregistré : c'était MiniLM, rien ne change
    state = ensure_vector_space(nl, 384, "all-MiniLM-L6-v2")
    assert state == {"dimension_changed": False, "model_changed": False, "reindex_required": False}
    assert get_meta(nl, "embedder_model") == "all-MiniLM-L6-v2"

    # Autre modèle de même dimension : vecteurs vidés
    state = ensure_vector_space(nl, 384, "bge-small-en-v1.5")
    assert state["model_changed"] and state["reindex_required"]

    # Autre dimension : type des colonnes changé, index recréés, réindexation possible
    state = ensure_vector_space(nl, 8, "Qwen3-Embedding-0.6B@8")
    assert state["dimension_changed"] and state["reindex_required"]
    assert current_dimension(nl, "nl2sql_tables") == 8 and current_dimension(nl, "nl2sql_columns") == 8

    def small_embed(texts):
        return [v[:8] for v in fake_embed(texts)]

    index_account(nl, src, account, small_embed, load_filter_config(CONFIG_PATH))
    assert ensure_vector_space(nl, 8, "Qwen3-Embedding-0.6B@8")["reindex_required"] is False

    # Remise à l'état d'origine pour les autres tests
    ensure_vector_space(nl, 384, "all-MiniLM-L6-v2")
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))


def test_odoo_module_is_recorded(dbs):
    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("SELECT table_name, odoo_module FROM nl2sql_tables WHERE account_id = %s ORDER BY 1",
                    (account["id"],))
        modules = dict(cur.fetchall())
    assert modules["sale_order"] == "sale" and modules["sale_order_line"] == "sale"
    assert modules["res_partner"] == "base"


def test_enrichment_export_import_roundtrip(dbs):
    import copy

    from schema_service.enrichment import export_schema, import_enrichment, list_modules

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    assert {m["module"]: m["tables"] for m in list_modules(nl, account["id"])}["sale"] == 2

    data = export_schema(nl, account["id"], module="sale")
    assert data["format"] == "askyourdata-enrichment"
    tables = {t["table_name"]: t for t in data["tables"]}
    assert set(tables) == {"sale_order", "sale_order_line"}
    so = tables["sale_order"]
    assert so["odoo_label"] == "Sales Order" and "sale_order_line.order_id" in so["referenced_by"]
    cols = {c["column_name"]: c for c in so["columns"]}
    assert "id" not in cols and "create_uid" not in cols                     # techniques masquées
    assert cols["partner_id"]["references"] == "res_partner.id"
    assert cols["state"]["values"].startswith("draft=Quotation")
    assert set(so["enrich"]) == {"label", "description", "synonyms", "domain"}   # valeurs actuelles pré-remplies

    with nl.cursor() as cur:
        cur.execute("""SELECT embedding FROM nl2sql_tables WHERE account_id = %s AND table_name = 'sale_order'""",
                    (account["id"],))
        before = cur.fetchone()[0]

    filled = copy.deepcopy(data)
    for t in filled["tables"]:
        if t["table_name"] == "sale_order":
            t["enrich"]["description"] = "Customer quotations and confirmed sales orders."
            t["enrich"]["synonyms"] = ["orders", "quotations", "sales"]
            t["enrich"]["unknown_field"] = "x"
            next(c for c in t["columns"] if c["column_name"] == "amount_total")["enrich"]["notes"] = \
                "Total including taxes"
    filled["tables"].append({"table_name": "does_not_exist", "enrich": {"description": "x"}})

    # Simulation : rapport complet, rien d'écrit
    report = import_enrichment(nl, account["id"], filled, dry_run=True)
    assert report["tables_changed"] == ["sale_order"] and report["fields_changed"] == 3
    assert report["unknown_tables"] == ["does_not_exist"]
    assert report["ignored_fields"] == ["table.unknown_field"]
    assert report["changed_table_ids"] == []
    assert export_schema(nl, account["id"], tables=["sale_order"])["tables"][0]["enrich"]["description"] == \
        so["enrich"]["description"]

    # Import réel
    report = import_enrichment(nl, account["id"], filled)
    assert len(report["changed_table_ids"]) == 1
    for table_id in report["changed_table_ids"]:
        reindex_table_embeddings(nl, table_id, fake_embed)
    again = export_schema(nl, account["id"], tables=["sale_order"])["tables"][0]
    assert again["enrich"]["synonyms"] == "orders, quotations, sales"
    assert again["enriched_by"] == "manual"
    with nl.cursor() as cur:
        cur.execute("""SELECT embedding FROM nl2sql_tables WHERE account_id = %s AND table_name = 'sale_order'""",
                    (account["id"],))
        after = cur.fetchone()[0]
        cur.execute("""SELECT v.source, v.text FROM nl2sql_vectors v JOIN nl2sql_tables t ON t.id = v.table_id
                       WHERE t.account_id = %s AND t.table_name = 'sale_order' AND v.column_id IS NULL
                       ORDER BY v.id""", (account["id"],))
        table_texts = cur.fetchall()
    # Fiche réencodée : le libellé (inchangé, « Orders ») puis un vecteur par nouveau synonyme
    # (« orders » = le libellé, non répété) ; la description n'est pas encodée
    assert str(before) == str(after)                                         # premier vecteur = libellé
    assert table_texts == [("label", "Orders"), ("synonym", "quotations"), ("synonym", "sales")]
    assert not any("Customer quotations" in t for _, t in table_texts)

    # Réimporter le même fichier ne change rien ; une réindexation ne l'écrase pas
    assert import_enrichment(nl, account["id"], filled)["fields_changed"] == 0
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    kept = export_schema(nl, account["id"], tables=["sale_order"])["tables"][0]
    assert kept["enrich"]["description"] == "Customer quotations and confirmed sales orders."

    # Chaîne vide : efface
    for t in filled["tables"]:
        if t["table_name"] == "sale_order":
            t["enrich"] = {"description": ""}
    import_enrichment(nl, account["id"], filled)
    assert export_schema(nl, account["id"], tables=["sale_order"])["tables"][0]["enrich"]["description"] is None


def test_enrichment_rejects_unknown_files(dbs):
    from schema_service.enrichment import import_enrichment

    _, nl, account = dbs
    with pytest.raises(ValueError):
        import_enrichment(nl, account["id"], {"tables": []})


def test_vector_ranking_orders_by_similarity(dbs):
    from schema_service.search_data import vector_ranking

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("SELECT embedding FROM nl2sql_tables WHERE account_id = %s AND table_name = 'res_country'",
                    (account["id"],))
        vec = cur.fetchone()[0]
    vec = vec.to_list() if hasattr(vec, "to_list") else [float(x) for x in vec]
    ranking = vector_ranking(nl, account["id"], vec, limit=3)
    assert ranking[0]["table_name"] == "res_country" and ranking[0]["similarity"] == pytest.approx(1.0, abs=1e-3)
    assert len(ranking) == 3


def test_column_similarities_per_piece(dbs):
    from schema_service.search_data import column_similarities

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("""SELECT c.embedding FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
                       WHERE t.account_id = %s AND t.table_name = 'sale_order' AND c.column_name = 'amount_total'""",
                    (account["id"],))
        vec = cur.fetchone()[0]
    amount_vec = vec.to_list() if hasattr(vec, "to_list") else [float(x) for x in vec]
    other = fake_embed(["customer"])[0]
    sims = column_similarities(nl, account["id"], ["sale_order", "res_partner"], [other, amount_vec])
    assert {t for t, _ in sims} == {"sale_order", "res_partner"}
    assert all(len(v) == 2 for v in sims.values())
    assert sims[("sale_order", "amount_total")][1] == pytest.approx(1.0, abs=1e-5)
    assert column_similarities(nl, account["id"], [], [other]) == {}


def test_always_include_is_imported_without_reencoding(dbs):
    import copy

    from schema_service.enrichment import export_schema, import_enrichment
    from schema_service.search_data import load_search_data

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    data = export_schema(nl, account["id"], tables=["sale_order"])
    state = next(c for c in data["tables"][0]["columns"] if c["column_name"] == "state")
    assert state["enrich"]["always_include"] is False

    filled = copy.deepcopy(data)
    next(c for c in filled["tables"][0]["columns"] if c["column_name"] == "state")["enrich"]["always_include"] = True
    report = import_enrichment(nl, account["id"], filled)
    assert report["tables_changed"] == ["sale_order"] and report["fields_changed"] == 1
    assert report["changed_table_ids"] == []                       # rien à réencoder

    _, _, columns_by_table, _, _ = load_search_data(nl, account["id"], fake_embed(["sales orders"])[0])
    flags = {c["column_name"]: c["always_include"] for c in columns_by_table["sale_order"]}
    assert flags["state"] is True and flags["amount_total"] is False

    # Une réindexation ne l'efface pas ; une valeur invalide est refusée
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    again = export_schema(nl, account["id"], tables=["sale_order"])["tables"][0]
    assert next(c for c in again["columns"] if c["column_name"] == "state")["enrich"]["always_include"] is True
    next(c for c in filled["tables"][0]["columns"] if c["column_name"] == "state")["enrich"]["always_include"] = "maybe"
    with pytest.raises(ValueError):
        import_enrichment(nl, account["id"], filled, dry_run=True)
    nl.rollback()


def test_extend_adds_the_missing_column_to_the_plan(dbs):
    """Recherche complémentaire sur la vraie base : le mot manquant ajoute sa colonne, rien n'est retiré."""
    from schema_service import structured
    from schema_service.search_data import run_structured

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))

    def stored(sql, params):
        with nl.cursor() as cur:
            cur.execute(sql, params)
            vec = cur.fetchone()[0]
        return vec.to_list() if hasattr(vec, "to_list") else [float(x) for x in vec]

    table_vec = stored("SELECT embedding FROM nl2sql_tables WHERE account_id = %s AND table_name = 'sale_order'",
                       (account["id"],))
    amount_vec = stored("""SELECT c.embedding FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
                           WHERE t.account_id = %s AND t.table_name = 'sale_order' AND c.column_name = 'amount_total'""",
                        (account["id"],))

    class FakeEmbedder:
        def embed_query(self, text, kind="tables"):
            return table_vec if text == "sales orders" else fake_embed([text])[0]

        def embed_queries(self, texts, kind="tables"):
            return [amount_vec if t == "order total amount" else self.embed_query(t, kind) for t in texts]

    sq = structured.StructuredQuestion.from_dict({"subject": "sales orders"})
    params = structured.StructuredParams()
    base, report = run_structured(nl, FakeEmbedder(), account["id"], "Average order value", sq, params)
    assert report == [] and base["tables"][0]["table_name"] == "sale_order"
    base_cols = {c["column_name"] for c in base["tables"][0]["columns"]}
    assert "amount_total" not in base_cols

    plan, report = run_structured(nl, FakeEmbedder(), account["id"], "Average order value", sq, params,
                                  missing=["order total amount"])
    assert report[0]["found"] == "column" and (report[0]["table"], report[0]["column"]) == ("sale_order", "amount_total")
    cols = {c["column_name"] for c in plan["tables"][0]["columns"]}
    assert "amount_total" in cols and base_cols <= cols
    assert {t["table_name"] for t in base["tables"]} <= {t["table_name"] for t in plan["tables"]}


def test_multi_vectors_score_by_best_synonym(dbs):
    """Une table et une colonne enrichies : un vecteur par libellé et par synonyme, score = le meilleur."""
    from schema_service.search_data import column_similarities, vector_ranking
    from schema_service.vector_space import missing_embeddings

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("""UPDATE nl2sql_tables SET label = 'Sales orders', synonyms = 'quotation, customer order',
                       manual_override = TRUE WHERE account_id = %s AND table_name = 'sale_order' RETURNING id""",
                    (account["id"],))
        so_id = cur.fetchone()[0]
        cur.execute("""UPDATE nl2sql_columns SET label = 'Customer', synonyms = 'client, buyer', manual_override = TRUE
                       WHERE table_id = %s AND column_name = 'partner_id'""", (so_id,))
    nl.commit()
    reindex_table_embeddings(nl, so_id, fake_embed)
    assert missing_embeddings(nl) == 0

    top = vector_ranking(nl, account["id"], fake_embed(["customer order"])[0], limit=3)[0]
    assert top == {"table_name": "sale_order", "similarity": 1.0, "matched": "customer order"}

    texts: dict = {}
    sims = column_similarities(nl, account["id"], ["sale_order"], fake_embed(["buyer", "Customer"]), texts)
    assert sims[("sale_order", "partner_id")] == pytest.approx([1.0, 1.0])
    assert texts[("sale_order", "partner_id")] == ["buyer", "Customer"]
    # Colonne non enrichie : une seule fiche complète (texte gagnant = la fiche)
    assert texts[("sale_order", "state")][0].startswith("Status")


def test_search_falls_back_to_single_vectors_before_reindex(dbs):
    """Index d'avant la v8 (aucun vecteur multiple) : la recherche utilise le vecteur unique ; /index requis."""
    from schema_service.search_data import column_similarities, vector_ranking
    from schema_service.vector_space import missing_embeddings

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        cur.execute("DELETE FROM nl2sql_vectors")
        cur.execute("""SELECT t.embedding, c.embedding FROM nl2sql_tables t JOIN nl2sql_columns c ON c.table_id = t.id
                       WHERE t.account_id = %s AND t.table_name = 'sale_order' AND c.column_name = 'amount_total'""",
                    (account["id"],))
        table_vec, col_vec = [[float(x) for x in (v.to_list() if hasattr(v, "to_list") else v)]
                              for v in cur.fetchone()]
    nl.commit()
    assert missing_embeddings(nl) > 0
    top = vector_ranking(nl, account["id"], table_vec, limit=1)[0]
    assert (top["table_name"], top["matched"]) == ("sale_order", None)
    sims = column_similarities(nl, account["id"], ["sale_order"], [col_vec])
    assert sims[("sale_order", "amount_total")][0] == pytest.approx(1.0)
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    assert missing_embeddings(nl) == 0


def test_entity_piece_takes_the_subject_foreign_key(dbs):
    """« by customer » rattaché à l'entité customer : la clé sale_order.partner_id, même si une autre
    colonne ressemble davantage au mot."""
    from schema_service import structured
    from schema_service.search_data import run_structured

    src, nl, account = dbs
    index_account(nl, src, account, fake_embed, load_filter_config(CONFIG_PATH))
    with nl.cursor() as cur:
        for table, label in (("sale_order", "Sales orders"), ("res_partner", "Contacts")):
            cur.execute("""UPDATE nl2sql_tables SET label = %s, synonyms = %s, manual_override = TRUE
                           WHERE account_id = %s AND table_name = %s RETURNING id""",
                        (label, "customer" if table == "res_partner" else "sales order", account["id"], table))
            table_id = cur.fetchone()[0]
            nl.commit()
            reindex_table_embeddings(nl, table_id, fake_embed)
        # Une colonne du sujet porte exactement le mot « customer » (piège) : elle ne doit pas gagner
        cur.execute("""UPDATE nl2sql_columns SET label = 'customer', manual_override = TRUE
                       WHERE column_name = 'state' AND table_id = (SELECT id FROM nl2sql_tables
                       WHERE account_id = %s AND table_name = 'sale_order') RETURNING table_id""", (account["id"],))
        so_id = cur.fetchone()[0]
    nl.commit()
    reindex_table_embeddings(nl, so_id, fake_embed)

    class Embedder:
        def embed_query(self, text, kind="tables"):
            return fake_embed([text])[0]

        def embed_queries(self, texts, kind="tables"):
            return fake_embed(list(texts))

    params = structured.StructuredParams(min_similarity=0.5, table_piece_min=0.5)
    plain = structured.StructuredQuestion.from_dict({"subject": "sales order", "group_by": ["customer"]})
    plan, _ = run_structured(nl, Embedder(), account["id"], "", plain, params)
    assert plan["pieces"][0]["matches"][0]["column"] == "state"            # sans entité : le mot seul

    sq = structured.StructuredQuestion.from_dict({"subject": "sales order", "group_by": ["customer"],
                                                  "entities": {"customer": "customer"}})
    plan, _ = run_structured(nl, Embedder(), account["id"], "", sq, params)
    piece = plan["pieces"][0]
    assert piece["entity"]["table"] == "res_partner" and piece["entity"]["rule"] == "fk"
    assert piece["matches"][0]["column"] == "partner_id"
    assert "res_partner" in {t["table_name"] for t in plan["tables"]}
