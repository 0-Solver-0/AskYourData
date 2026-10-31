"""Accès public par clé : réglages, blocage des essais, quotas, journal, routes fermées derrière le proxy."""

import os
import uuid

import pytest

from api_service import access

DSN = os.getenv("TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN non défini (PostgreSQL requis)")


def test_settings_and_keys():
    s = access.Settings.from_env({"PUBLIC_MODE": "true", "ACCESS_DEFAULT_QUOTA": "20",
                                  "ACCESS_GLOBAL_BUDGET_USD": "20", "ACCESS_MIN_INTERVAL_S": "x"})
    assert (s.public, s.default_quota, s.global_budget_usd, s.min_interval_s) == (True, 20, 20.0, 5.0)
    assert access.Settings.from_env({}).public is False
    key = access.new_key()
    assert key.startswith("ayd_") and len(key) == 36 and access.new_key() != key
    assert access.hash_key(key) == access.hash_key(f" {key} ") and len(access.hash_key(key)) == 64


def test_guard_blocks_an_address_after_five_wrong_keys():
    now = [0.0]
    guard = access.Guard(clock=lambda: now[0])
    for _ in range(access.FAILED_KEY_LIMIT):
        guard.check_blocked("1.2.3.4")
        guard.failed("1.2.3.4")
    with pytest.raises(access.AccessError) as exc:
        guard.check_blocked("1.2.3.4")
    assert exc.value.status == 429
    guard.check_blocked("5.6.7.8")                     # une autre adresse n'est pas touchée
    now[0] += access.FAILED_KEY_BLOCK_S + 1
    guard.check_blocked("1.2.3.4")                     # débloquée après 15 minutes


@pytest.fixture
def db():
    import psycopg2

    admin = psycopg2.connect(DSN, dbname="postgres")
    admin.autocommit = True
    name = f"t_access_{uuid.uuid4().hex[:8]}"
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(DSN, dbname=name)
    access._schema_ready = False
    access.ensure_schema(conn)
    yield conn, f"{DSN} dbname={name}"
    conn.close()
    access._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {name}")
    admin.close()


@needs_pg
def test_quota_interval_budget_and_log(db):
    conn, _ = db
    settings = access.Settings(public=True, default_quota=2, global_budget_usd=0.05, min_interval_s=0)
    guard = access.Guard()
    key, _ = access.create_key(conn, "Karim (LinkedIn)", 2)
    with conn.cursor() as cur:
        cur.execute("SELECT key_hash FROM access_keys")
        assert cur.fetchone()[0] == access.hash_key(key)            # jamais la clé en clair

    with pytest.raises(access.AccessError) as exc:
        access.check_key(conn, "ayd_wrong", "9.9.9.9", guard, settings)
    assert exc.value.code == "invalid_key"

    found = access.check_key(conn, key, "9.9.9.9", guard, settings)
    found = access.reserve_question(conn, found, settings)
    assert access.remaining(found) == 1
    access.record(conn, found["id"], "ask", question="Top 5 customers", rewritten=None, status="ok", cost_usd=0.01)
    found = access.reserve_question(conn, access.check_key(conn, key, "9.9.9.9", guard, settings), settings)
    assert access.remaining(found) == 0
    with pytest.raises(access.AccessError) as exc:
        access.reserve_question(conn, access.check_key(conn, key, "9.9.9.9", guard, settings), settings)
    assert exc.value.code == "quota"

    # Délai minimal entre deux questions d'une même clé
    other, _ = access.create_key(conn, "Jury Meta", 50)
    slow = access.Settings(public=True, default_quota=20, global_budget_usd=0.05, min_interval_s=60)
    access.reserve_question(conn, access.check_key(conn, other, "8.8.8.8", guard, slow), slow)
    with pytest.raises(access.AccessError) as exc:
        access.reserve_question(conn, access.check_key(conn, other, "8.8.8.8", guard, slow), slow)
    assert exc.value.code == "too_fast"
    access.touch_for_voice(conn, access.check_key(conn, other, "8.8.8.8", guard, slow), slow)  # voix : compteur à part

    # Plafond global atteint : tout est bloqué, même une clé avec des questions restantes
    access.record(conn, found["id"], "transcribe", question="voice", status="ok", cost_usd=0.05)
    with pytest.raises(access.AccessError) as exc:
        access.check_key(conn, other, "8.8.8.8", guard, settings)
    assert exc.value.code == "budget"

    stats = access.stats(conn, settings)
    assert stats["totals"]["questions"] == 1 and stats["totals"]["transcriptions"] == 1
    assert stats["active_now"] == 2 and stats["questions"][0]["name"] == "Karim (LinkedIn)"
    assert [q["question"] for q in access.export_questions(conn)] == ["Top 5 customers"]
    assert "name" not in access.export_questions(conn)[0]

    assert access.forget(conn, "Karim (LinkedIn)") == 2
    with pytest.raises(access.AccessError) as exc:
        access.check_key(conn, key, "9.9.9.9", guard, access.Settings(True, 2, 100, 0))
    assert exc.value.code == "revoked"


@needs_pg
def test_public_mode_routes(db, monkeypatch):
    conn, dsn = db
    from api_service import app as app_module

    monkeypatch.setenv("ACCESS_DB_DSN", dsn)
    monkeypatch.setattr(app_module, "ACCESS", access.Settings(True, 20, 20.0, 0))
    monkeypatch.setattr(app_module, "GUARD", access.Guard())
    monkeypatch.setattr(app_module, "get_llm", lambda: object())
    seen = {}

    def fake_ask(client, schema_fn, execute_fn, question, account_id, **kwargs):
        seen.update(question=question, account_id=account_id, structured=kwargs.get("structured"))
        return {"status": "ok", "rewritten": "Top 5 customers.", "cost_usd": 0.007, "timings_ms": {"total": 900},
                "executed_sql": "SELECT 1", "answer": {"summary": "x", "columns": [], "rows": [], "row_count": 0}}

    monkeypatch.setattr(app_module.pipeline, "ask", fake_ask)
    key, _ = access.create_key(conn, "Jury Meta", 50)
    client = app_module.app.test_client()
    proxy = {"X-Forwarded-For": "203.0.113.7"}

    # Derrière le proxy : seules l'interface et les routes de question existent
    for path in ("/api/v1/index", "/api/v1/schema/export", "/api/v1/translate", "/admin", "/api/v1/admin/stats",
                 "/classic", "/static/admin.html", "/api/v1/llm"):
        resp = client.get(path, headers=proxy) if "index" not in path else client.post(path, headers=proxy, json={})
        assert resp.status_code == 404, path
    assert client.get("/robots.txt", headers=proxy).data == b"User-agent: *\nDisallow: /\n"
    assert client.get("/", headers=proxy).status_code == 200

    # Sans clé : rien n'est accepté
    assert client.post("/api/v1/ask", headers=proxy, json={"question": "q"}).status_code == 401
    assert client.get("/api/v1/access/me", headers=proxy).json["code"] == "invalid_key"

    # Avec la clé : la question part, le compte et le découpage sont imposés, tout est journalisé
    headers = {**proxy, "X-Access-Key": key}
    assert client.get("/api/v1/access/me", headers=headers).json["remaining"] == 50
    resp = client.post("/api/v1/ask", headers=headers,
                       json={"question": "top 5 customers", "account_id": 99, "structured": {"subject": "x"}})
    assert resp.status_code == 200 and resp.json["access"]["remaining"] == 49
    assert seen == {"question": "top 5 customers", "account_id": 1, "structured": None}
    assert client.post("/api/v1/ask", headers=headers, json={"question": "x" * 501}).status_code == 400
    with conn.cursor() as cur:
        cur.execute("SELECT question, rewritten, status, executed_sql FROM usage_log")
        assert cur.fetchall() == [("top 5 customers", "Top 5 customers.", "ok", "SELECT 1")]

    # En direct sur le serveur (tunnel SSH) : le tableau de bord répond
    stats = client.get("/api/v1/admin/stats")
    assert stats.status_code == 200 and stats.json["totals"]["questions"] == 1
