"""Rôle SQL et chaîne translate (découpage → schéma → SQL) — faux modèle, faux schema_service."""

import json

from api_service import llm, pipeline, sqlgen
from tests.test_llm import ENV, PROFILES_PATH, FakeTransport, ok_body

DDL = "CREATE TABLE sale_order (  -- Sales orders\n    id integer PRIMARY KEY,\n    amount_total numeric\n);"
PREP_OK = {"status": "ok", "reason": None, "subject": "sales orders", "measures": ["revenue"], "filters": [],
           "group_by": ["customer"], "period": "order date", "order": "revenue desc, top 5"}


def client(*responses):
    return llm.LLMClient(llm.load_profiles(PROFILES_PATH), llm.roles_from_env(ENV), env=ENV,
                         transport=FakeTransport(*responses), retry_delay_s=0)


def schema_ok(account_id, question, structured):
    return {"tables": [{"table_name": "sale_order"}, {"table_name": "res_partner"}], "schema_ddl": DDL,
            "token_estimate": 40, "debug": {"pieces": [{"text": "carrier", "not_found": True}]}}


def test_prompt_and_user_message():
    prompt = sqlgen.load_prompt(sqlgen.PROMPT_PATH)
    assert prompt.startswith("You write one PostgreSQL query") and "<missing>" in prompt and "```" not in prompt
    msg = sqlgen.build_user_message("Top 5 customers", {"subject": "sales orders", "filters": []}, DDL)
    assert msg.startswith("Question: Top 5 customers")
    assert 'Breakdown: {"subject": "sales orders"}' in msg and "CREATE TABLE sale_order" in msg
    again = sqlgen.build_user_message("q", {}, DDL, previous={"missing": ["stock value"]})
    assert 'You asked for: ["stock value"]' in again


def test_answers_are_parsed():
    assert sqlgen.parse_answer("<sql>\nSELECT 1;\n</sql>") == {"status": "sql", "sql": "SELECT 1",
                                                              "missing": [], "reason": None}
    assert sqlgen.parse_answer("Here:\n```sql\nSELECT 2\n```")["sql"] == "SELECT 2"
    miss = sqlgen.parse_answer('<missing>["product category of the sold product"]</missing>')
    assert miss["status"] == "missing" and miss["missing"] == ["product category of the sold product"]
    assert sqlgen.parse_answer("<missing>stock value</missing>")["missing"] == ["stock value"]
    cannot = sqlgen.parse_answer("<cannot_answer>No carrier data is recorded.</cannot_answer>")
    assert cannot["status"] == "cannot_answer" and cannot["reason"] == "No carrier data is recorded."
    assert sqlgen.parse_answer("I think the answer is 42")["status"] == "error"


def test_translate_end_to_end():
    c = client((200, ok_body(text=json.dumps(PREP_OK)), ""),
               (200, ok_body(text="<sql>SELECT 1</sql>", model="muse-spark-1.3"), ""))
    out = pipeline.translate(c, schema_ok, "Top 5 customers by revenue", 1)
    assert out["status"] == "sql" and out["sql"] == "SELECT 1"
    assert out["prep_status"] == "ok" and out["structured_request"]["subject"] == "sales orders"
    assert out["tables_used"] == ["sale_order", "res_partner"] and out["not_found"] == ["carrier"]
    assert set(out["usage"]) == {"prep", "sql"} and out["cost_usd"] > 0
    assert {"structuring", "schema", "generation", "total"} <= set(out["timings_ms"])
    sent = c.transport.calls[1]["payload"]["messages"]
    assert sent[0]["content"].startswith("You write one PostgreSQL query") and DDL in sent[1]["content"]


def test_translate_with_a_given_breakdown_skips_prep():
    c = client((200, ok_body(text="<sql>SELECT 1</sql>"), ""))
    out = pipeline.translate(c, schema_ok, "q", 1, structured={"subject": "sales orders"})
    assert out["prep_status"] == "provided" and "prep" not in out["usage"] and len(c.transport.calls) == 1


def test_refusal_stops_before_the_schema():
    refusal = {"status": "write_request", "reason": "I can only read data.", "subject": None}
    calls = []
    out = pipeline.translate(client((200, ok_body(text=json.dumps(refusal)), "")),
                             lambda *a: calls.append(a) or {}, "Delete all quotations", 1)
    assert out["status"] == "refused" and out["reason"] == "I can only read data." and not calls


def test_schema_error_and_missing_answer():
    out = pipeline.translate(client(), lambda *a: {"error": "Aucune table indexée"}, "q", 1,
                             structured={"subject": "x"})
    assert out["status"] == "error" and "Aucune table" in out["reason"]
    c = client((200, ok_body(text='<missing>["stock value"]</missing>'), ""))
    out = pipeline.translate(c, schema_ok, "Stock value by category", 1, structured={"subject": "products"})
    assert out["status"] == "missing" and out["missing"] == ["stock value"] and out["sql"] is None
