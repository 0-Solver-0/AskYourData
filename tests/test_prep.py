"""Rôle PREP : prompt, normalisation de la réponse, repli — sans appel réseau."""

import json

import pytest

from api_service import llm, prep
from tests.test_llm import ENV, PROFILES_PATH, FakeTransport, ok_body


def client(*responses):
    return llm.LLMClient(llm.load_profiles(PROFILES_PATH), llm.roles_from_env(ENV), env=ENV,
                         transport=FakeTransport(*responses), retry_delay_s=0)


def test_prompt_is_read_from_the_markdown_block():
    prompt = prep.load_prompt()
    assert prompt.startswith("You split one business question")
    assert '"status"' in prompt and "Output only the JSON object" in prompt
    assert "```" not in prompt


def test_answer_is_normalized():
    raw = {"status": "ok", "reason": None, "subject": " customer  invoices ", "measures": "amount due",
           "filters": ["payment status", "", None], "group_by": ["customer"], "period": None, "order": None}
    out = prep.normalize(raw)
    assert out["status"] == "ok"
    assert out["structured"] == {"subject": "customer invoices", "measures": ["amount due"],
                                 "filters": ["payment status"], "group_by": ["customer"],
                                 "period": None, "order": None}
    # Tableau d'un seul objet (ancien format) accepté ; statut inconnu signalé
    out = prep.normalize([{"status": "weird", "subject": "employees"}])
    assert out["status"] == "ok" and out["warnings"]
    with pytest.raises(ValueError):
        prep.normalize({"status": "ok", "subject": None})


def test_structure_question_end_to_end_with_a_fake_model():
    answer = {"status": "ok", "reason": None, "subject": "sales orders", "measures": ["revenue"],
              "filters": [], "group_by": ["customer"], "period": "order date", "order": "revenue desc, top 5"}
    c = client((200, ok_body(text="```json\n" + json.dumps(answer) + "\n```"), ""))
    out = prep.structure_question(c, "Top 5 customers by revenue this year")
    assert out["status"] == "ok" and out["structured"]["subject"] == "sales orders"
    assert out["llm"]["provider"] == "meta_spark" and out["llm"]["cost_usd"] > 0
    sent = c.transport.calls[0]["payload"]["messages"]
    assert sent[0]["role"] == "system" and sent[1]["content"] == "Question: Top 5 customers by revenue this year"


def test_refusals_keep_their_reason():
    answer = {"status": "write_request", "reason": "I can only read data.", "subject": None,
              "measures": [], "filters": [], "group_by": [], "period": None, "order": None}
    out = prep.structure_question(client((200, ok_body(text=json.dumps(answer)), "")), "Delete all quotations")
    assert out["status"] == "write_request" and out["reason"] == "I can only read data."


def test_unreadable_or_failed_call_degrades_to_the_raw_question():
    out = prep.structure_question(client((200, ok_body(text="sorry, no idea"), "")), "Top customers")
    assert out["status"] == "degraded" and out["structured"]["subject"] == "Top customers"
    assert out["raw"] == "sorry, no idea"
    failing = client((503, None, ""), (503, None, ""), (503, None, ""), (503, None, ""))
    out = prep.structure_question(failing, "Top customers")
    assert out["status"] == "degraded" and "PREP indisponible" in out["reason"]
    assert len(out["llm"]["attempts"]) == 4
