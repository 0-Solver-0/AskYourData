"""Juge du sujet (faux modèle) et son déclenchement dans la chaîne translate."""

import json

from api_service import judge, pipeline
from tests.test_llm import ok_body
from tests.test_sqlgen import DDL, PREP_OK, client

RANKING = [{"table_name": "hr_work_location", "similarity": 0.545, "label": "Work locations"},
           {"table_name": "hr_employee", "similarity": 0.530, "label": "Employees",
            "description": "One row per employee"},
           {"table_name": "hr_department", "similarity": 0.47, "label": "Departments"}]


def test_judge_is_called_only_when_the_first_tables_are_close():
    assert judge.should_judge(RANKING, 0.05)                     # écart 0,015
    assert not judge.should_judge(RANKING, 0.01)
    assert not judge.should_judge(RANKING, 0)                    # 0 : désactivé
    assert not judge.should_judge(RANKING[:1], 0.05)
    msg = judge.build_user_message("Headcount per department", {"subject": "employees"}, RANKING)
    assert "Subject words: employees" in msg and "- hr_employee: Employees — One row per employee" in msg


def test_judge_answer_must_be_a_candidate():
    c = client((200, ok_body(text='{"subject": "hr_employee", "reason": "headcount counts employees"}'), ""))
    out = judge.pick_subject(c, "Headcount per department", {"subject": "employees"}, RANKING)
    assert out["subject"] == "hr_employee" and out["candidates"][0] == "hr_work_location"
    assert c.transport.calls[0]["payload"]["messages"][0]["content"].startswith("You pick the database table")
    outside = judge.pick_subject(client((200, ok_body(text='{"subject": "hr_contract"}'), "")), "q", {}, RANKING)
    assert outside["subject"] is None and "hors des candidates" in outside["reason"]
    unreadable = judge.pick_subject(client((200, ok_body(text="I think employees"), "")), "q", {}, RANKING)
    assert unreadable["subject"] is None


def schema_with(ranking, subject_table):
    calls = []

    def fn(account_id, question, structured, subject=None):
        calls.append(subject)
        chosen = subject or subject_table
        return {"tables": [{"table_name": chosen}], "schema_ddl": DDL + f"\n-- {chosen}", "token_estimate": 50,
                "debug": {"subject_ranking": ranking, "subject_table": chosen,
                          "subject_by": "forced" if subject else "vectors", "pieces": []}}
    fn.calls = calls
    return fn


def test_translate_asks_the_judge_and_rebuilds_the_schema():
    c = client((200, ok_body(text=json.dumps(PREP_OK)), ""),
               (200, ok_body(text='{"subject": "hr_employee", "reason": "employees"}'), ""),
               (200, ok_body(text="<sql>SELECT 1</sql>"), ""))
    fn = schema_with(RANKING, "hr_work_location")
    out = pipeline.translate(c, fn, "Headcount per department", 1, gap=0.05)
    assert fn.calls == [None, "hr_employee"]
    assert out["subject_choice"]["by"] == "judge" and out["subject_choice"]["table"] == "hr_employee"
    assert out["subject_choice"]["vectors"] == "hr_work_location"
    assert out["tables_used"] == ["hr_employee"] and "judge" in out["usage"] and "judge" in out["timings_ms"]
    assert out["status"] == "sql"


def test_no_judge_when_the_ranking_is_clear():
    clear = [{"table_name": "sale_order", "similarity": 0.72}, {"table_name": "crm_team", "similarity": 0.5}]
    c = client((200, ok_body(text="<sql>SELECT 1</sql>"), ""))
    fn = schema_with(clear, "sale_order")
    out = pipeline.translate(c, fn, "q", 1, structured={"subject": "sales orders"}, gap=0.05)
    assert fn.calls == [None] and out["subject_choice"]["by"] == "vectors" and "judge" not in out["usage"]


def test_judge_failure_keeps_the_vector_choice():
    c = client((200, ok_body(text="no idea"), ""), (200, ok_body(text="<sql>SELECT 1</sql>"), ""))
    fn = schema_with(RANKING, "hr_work_location")
    out = pipeline.translate(c, fn, "q", 1, structured={"subject": "employees"}, gap=0.05)
    assert fn.calls == [None] and out["subject_choice"]["table"] == "hr_work_location" and out["status"] == "sql"
