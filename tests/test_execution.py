"""Vérification du SQL, réponse par gabarit, chaîne ask (faux modèle, fausse exécution)."""

import json

from api_service import answer, pipeline, validate
from tests.test_llm import ok_body
from tests.test_sqlgen import PREP_OK, client

DDL = """CREATE TABLE sale_order (  -- Sales orders
    id integer PRIMARY KEY,
    partner_id integer,  -- Customer
    amount_total numeric,  -- Total
    state character varying,  -- values: draft, sent, sale
    date_order timestamp without time zone,
    FOREIGN KEY (partner_id) REFERENCES res_partner(id)
);
CREATE TABLE res_partner (  -- Contacts
    id integer PRIMARY KEY,
    name character varying
);
CREATE TABLE res_users (
    id integer PRIMARY KEY,
    login character varying
);"""


def ok(sql):
    return validate.validate_sql(sql, DDL)


def test_schema_is_read_from_the_ddl():
    schema = validate.schema_from_ddl(DDL)
    assert set(schema) == {"sale_order", "res_partner", "res_users"}
    assert set(schema["sale_order"]) == {"id", "partner_id", "amount_total", "state", "date_order"}


def test_valid_queries_pass_and_get_a_limit():
    sql = ("SELECT p.name AS customer, SUM(so.amount_total) AS total_revenue FROM sale_order so "
           "JOIN res_partner p ON p.id = so.partner_id WHERE so.state = 'sale' "
           "AND so.date_order >= date_trunc('year', CURRENT_DATE) GROUP BY p.name ORDER BY total_revenue DESC")
    check = ok(sql)
    assert check["ok"] and check["sql"].endswith("\nLIMIT 1000") and check["tables"] == ["res_partner", "sale_order"]
    assert ok(sql + " LIMIT 5")["sql"].endswith("LIMIT 5")
    cte = ("WITH t AS (SELECT partner_id, count(*) AS n FROM sale_order GROUP BY partner_id) "
           "SELECT p.name, t.n FROM t JOIN res_partner p ON p.id = t.partner_id ORDER BY n DESC LIMIT 10")
    assert ok(cte)["ok"]
    assert ok("SELECT count(*) FILTER (WHERE so.state = 'sale') AS n FROM sale_order so")["ok"]


def test_writes_and_tricks_are_refused():
    for sql, word in (("DELETE FROM sale_order", "only SELECT"),
                      ("WITH d AS (DELETE FROM sale_order RETURNING id) SELECT id FROM d", "DELETE"),
                      ("SELECT 1; SELECT 2", "exactly one statement"),
                      ("SELECT id INTO copy_so FROM sale_order", "INTO"),
                      ("SELECT id FROM sale_order FOR UPDATE", "LOCK"),
                      ("SELECT pg_sleep(10)", "pg_sleep"),
                      ("SELECT query_to_xml('select 1', true, true, '')", "query_to_xml"),
                      ("SELECT * FROM sale_order", "SELECT *"),
                      ("SELECT p.* FROM res_partner p", "SELECT *")):
        check = ok(sql)
        assert not check["ok"] and word in check["error"], (sql, check)


def test_tables_and_columns_must_be_in_the_schema():
    assert "not in the schema" in ok("SELECT id FROM account_move")["error"]
    assert "not allowed" in ok("SELECT relname FROM pg_catalog.pg_class")["error"]
    assert "Unknown column" in ok("SELECT so.amount_untaxed FROM sale_order so")["error"]
    assert "could not be resolved" in ok("SELECT margin FROM sale_order")["error"]
    assert "sensitive" in ok("SELECT u.password FROM res_users u")["error"]
    assert "sensitive" in ok("SELECT key FROM res_users_apikeys")["error"]


def test_summary_uses_only_the_values_of_the_result():
    assert answer.summarize(["total_revenue"], [[125430.5]]) == "Total revenue: 125,430.50."
    assert answer.summarize(["customer", "total_revenue"], []) == "No data matches this question."
    text = answer.summarize(["customer", "total_revenue"], [["Azure Interior", 9800], ["Deco Addict", 5000]])
    assert text == "2 rows. First: Azure Interior (Total revenue 9,800)."
    built = answer.build_answer({"columns": ["n"], "rows": [[3]], "truncated": False})
    assert built["row_count"] == 1 and built["summary"] == "N: 3."


def schema_fn(account_id, question, structured):
    return {"tables": [{"table_name": "sale_order"}, {"table_name": "res_partner"}], "schema_ddl": DDL,
            "token_estimate": 80, "debug": {"pieces": []}}


def fake_execute(*results):
    calls = []

    def run(sql):
        calls.append(sql)
        return results[len(calls) - 1]
    run.calls = calls
    return run


ROWS = {"ok": True, "columns": ["customer", "total_revenue"], "rows": [["Azure Interior", 9800]],
        "row_count": 1, "truncated": False, "error": None, "error_kind": None, "plan_cost": 12.5, "elapsed_ms": 3}
GOOD = ("SELECT p.name AS customer, SUM(so.amount_total) AS total_revenue FROM sale_order so "
        "JOIN res_partner p ON p.id = so.partner_id GROUP BY p.name ORDER BY total_revenue DESC LIMIT 5")


def test_ask_executes_and_answers():
    c = client((200, ok_body(text=json.dumps(PREP_OK)), ""), (200, ok_body(text=f"<sql>{GOOD}</sql>"), ""))
    run = fake_execute(ROWS)
    out = pipeline.ask(c, schema_fn, run, "Top 5 customers by revenue", 1)
    assert out["status"] == "ok" and out["executed_sql"] == GOOD and run.calls == [GOOD]
    assert out["answer"]["rows"] == [["Azure Interior", 9800]]
    assert out["answer"]["summary"] == "1 result: Customer Azure Interior, Total revenue 9,800."
    assert {"validation", "execution", "total"} <= set(out["timings_ms"])


def test_invalid_query_is_corrected_with_the_exact_error():
    bad = "SELECT so.margin FROM sale_order so"
    c = client((200, ok_body(text=f"<sql>{bad}</sql>"), ""), (200, ok_body(text=f"<sql>{GOOD}</sql>"), ""))
    run = fake_execute(ROWS)
    out = pipeline.ask(c, schema_fn, run, "q", 1, structured={"subject": "sales orders"})
    assert out["status"] == "ok" and [a["kind"] for a in out["attempts"]] == ["generation", "correction"]
    assert out["checks"][0]["valid"] is False and len(run.calls) == 1        # la requête fausse n'est pas exécutée
    second = c.transport.calls[1]["payload"]["messages"][1]["content"]
    assert bad in second and "Unknown column" in second


def test_database_error_is_corrected_then_gives_up_after_two():
    db_error = dict(ROWS, ok=False, rows=[], error="ERROR: operator does not exist: jsonb = text", error_kind="sql")
    c = client(*[(200, ok_body(text=f"<sql>{GOOD}</sql>"), "")] * 3)
    run = fake_execute(db_error, db_error, db_error)
    out = pipeline.ask(c, schema_fn, run, "q", 1, structured={"subject": "x"})
    assert out["status"] == "error" and "operator does not exist" in out["reason"]
    assert len(c.transport.calls) == 3 and len(run.calls) == 3             # 1 génération + 2 corrections
    assert "operator does not exist" in c.transport.calls[2]["payload"]["messages"][1]["content"]


def test_missing_password_stops_without_correction():
    config = dict(ROWS, ok=False, rows=[], error="EXEC_DB_PASSWORD manquant", error_kind="config")
    c = client((200, ok_body(text=f"<sql>{GOOD}</sql>"), ""))
    out = pipeline.ask(c, schema_fn, fake_execute(config), "q", 1, structured={"subject": "x"})
    assert out["status"] == "error" and "EXEC_DB_PASSWORD" in out["reason"] and len(c.transport.calls) == 1


def test_no_sql_means_no_execution():
    c = client((200, ok_body(text="<cannot_answer>No carrier data.</cannot_answer>"), ""))
    run = fake_execute()
    out = pipeline.ask(c, schema_fn, run, "q", 1, structured={"subject": "x"})
    assert out["status"] == "cannot_answer" and out["answer"] is None and run.calls == []
