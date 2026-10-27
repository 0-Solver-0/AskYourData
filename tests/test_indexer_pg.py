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
