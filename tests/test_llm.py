"""Client LLM par rôle : profils, secours, extraction, coût — avec un faux transport (aucun appel réseau)."""

import os

import pytest

from api_service import llm

PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "llm_providers.yaml")
ENV = {"PREP_PROVIDER": "meta_spark", "PREP_FALLBACK": "baseten_spark_open", "PREP_REASONING": "low",
       "SQL_PROVIDER": "meta_spark", "META_API_KEY": "sk-meta-secret", "BASETEN_API_KEY": "sk-baseten"}


def ok_body(text="OK", model="muse-spark-1.3", finish="stop", cached=0):
    return {"model": model, "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                      "prompt_tokens_details": {"cached_tokens": cached},
                      "completion_tokens_details": {"reasoning_tokens": 150}}}


class FakeTransport:
    """Répond dans l'ordre avec les réponses données ; garde les appels reçus."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(transport, env=None):
    env = ENV if env is None else env
    return llm.LLMClient(llm.load_profiles(PROFILES_PATH), llm.roles_from_env(env), env=env,
                         transport=transport, retry_delay_s=0)


def test_profiles_file_is_valid_and_has_no_secret():
    profiles = llm.load_profiles(PROFILES_PATH)
    assert profiles["meta_spark"].model == "muse-spark-1.3"
    assert profiles["meta_spark"].base_url == "https://api.meta.ai/v1"
    assert profiles["meta_spark"].reasoning_param == "reasoning_effort"
    text = open(PROFILES_PATH, encoding="utf-8").read()
    assert "sk-" not in text and "api_key:" not in text      # seulement des noms de variables


def test_successful_call_builds_the_request_and_telemetry():
    t = FakeTransport((200, ok_body(cached=400), ""))
    res = client(t).complete("prep", [{"role": "user", "content": "hi"}], max_tokens=300)
    call = t.calls[0]
    assert call["url"] == "https://api.meta.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-meta-secret"
    assert call["payload"]["model"] == "muse-spark-1.3"
    assert call["payload"]["reasoning_effort"] == "low" and call["payload"]["max_tokens"] == 300
    assert res.text == "OK" and res.provider == "meta_spark" and not res.fallback_used
    assert (res.tokens_in, res.tokens_cached, res.tokens_out, res.tokens_reasoning) == (1000, 400, 200, 150)
    # 600 × 1,25 + 400 × 0,15 + 200 × 4,25 = 1660 $ par million
    assert res.cost_usd == pytest.approx(0.00166)
    assert "sk-meta-secret" not in str(res.telemetry())


def test_transient_error_is_retried_on_the_same_profile():
    t = FakeTransport((503, {"error": "busy"}, ""), (200, ok_body(), ""))
    res = client(t).complete("prep", [{"role": "user", "content": "hi"}])
    assert res.provider == "meta_spark" and len(t.calls) == 2 and len(res.attempts) == 2


def test_fallback_after_two_failures_or_a_hard_error():
    t = FakeTransport(TimeoutError("timed out"), (502, None, "bad gateway"),
                      (200, ok_body(model="meta-models/muse-spark-open-1026"), ""))
    res = client(t).complete("prep", [{"role": "user", "content": "hi"}])
    assert res.provider == "baseten_spark_open" and res.fallback_used
    assert t.calls[2]["headers"]["Authorization"] == "Bearer sk-baseten"
    # Clé refusée : pas de nouvel essai, secours directement
    t = FakeTransport((401, {"error": "invalid key"}, ""), (200, ok_body(), ""))
    res = client(t).complete("prep", [{"role": "user", "content": "hi"}])
    assert res.fallback_used and len(t.calls) == 2


def test_missing_key_skips_to_the_fallback_and_errors_hide_the_key():
    env = {**ENV, "META_API_KEY": ""}
    t = FakeTransport((200, ok_body(), ""))
    res = client(t, env).complete("prep", [{"role": "user", "content": "hi"}])
    assert res.provider == "baseten_spark_open"
    assert "clé absente (META_API_KEY)" in res.attempts[0]["error"]
    # Rôle sql sans secours : l'échec est explicite, la clé n'apparaît pas
    t = FakeTransport((400, {"error": "bad request with sk-meta-secret"}, ""))
    with pytest.raises(llm.LLMError) as exc:
        client(t).complete("sql", [{"role": "user", "content": "hi"}])
    assert "sk-meta-secret" not in str(exc.value) and "***" in str(exc.value)


def test_truncated_answer_is_an_explicit_error():
    t = FakeTransport((200, ok_body(text=None, finish="length"), ""), (200, ok_body(text="", finish="length"), ""))
    with pytest.raises(llm.LLMError) as exc:
        client(t).complete("prep", [{"role": "user", "content": "hi"}])
    assert "plafond max_tokens" in str(exc.value)
    assert len(t.calls) == 2       # pas de nouvel essai sur le même profil, un essai sur le secours


def test_unconfigured_role_and_unknown_profile():
    with pytest.raises(llm.LLMError):
        client(FakeTransport(), {"PREP_PROVIDER": "meta_spark"}).complete("sql", [])
    with pytest.raises(ValueError):
        client(FakeTransport(), {"PREP_PROVIDER": "does_not_exist"})


def test_describe_reports_keys_without_values():
    desc = client(FakeTransport()).describe()
    assert desc["prep"]["primary"]["api_key_set"] is True
    assert desc["prep"]["fallback"]["profile"] == "baseten_spark_open"
    assert "sk-" not in str(desc)


def test_extraction_is_tolerant():
    assert llm.extract_json('Sure! ```json\n[{"a": 1}]\n``` done') == [{"a": 1}]
    assert llm.extract_json('prefix {"subject": "x", "n": [1, 2]} suffix') == {"subject": "x", "n": [1, 2]}
    with pytest.raises(ValueError):
        llm.extract_json("no json here")
    text = "thinking…\n<sql>\nSELECT 1\n</sql>"
    assert llm.extract_tag(text, "sql") == "SELECT 1"
    assert llm.extract_tag("<missing>[\"product category\"]</missing>", "missing") == '["product category"]'
    assert llm.extract_tag("nothing", "sql") is None
