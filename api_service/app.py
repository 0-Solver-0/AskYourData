"""
api_service/app.py
------------------
Orchestrateur du pipeline NL2SQL v2.

Pipeline :
  1. rewrite_service  — traduit et clarifie la question
  2. schema_service   — identifie tables, colonnes, relations
  3. model_service    — génère le SQL sur schéma minimal

Endpoints :
  POST /api/v1/translate   — pipeline complet NL → SQL
  GET  /api/v1/health      — healthcheck tous les services
  GET  /api/v1/schemas     — liste les comptes disponibles
  POST /api/v1/index       — indexe les tables d'un compte
  POST /api/v1/annotate/table   — annoter une table
  POST /api/v1/annotate/column  — annoter une colonne
"""

import os
import logging
import requests

from flask import Flask, request, jsonify

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("api_service")

# ── Config ─────────────────────────────────────────────────────────────────
MODEL_SERVICE_URL   = os.getenv("MODEL_SERVICE_URL",   "http://model_service:8001")
REWRITE_SERVICE_URL = os.getenv("REWRITE_SERVICE_URL", "http://rewrite_service:8003")
SCHEMA_SERVICE_URL  = os.getenv("SCHEMA_SERVICE_URL",  "http://schema_service:8002")
SECRET_KEY          = os.getenv("SECRET_KEY",          "dev-secret")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


# ── Helpers ─────────────────────────────────────────────────────────────────

def call_rewrite(question: str) -> dict:
    resp = requests.post(
        f"{REWRITE_SERVICE_URL}/rewrite",
        json={"question": question},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def call_schema(question: str, account_id: int) -> dict:
    resp = requests.post(
        f"{SCHEMA_SERVICE_URL}/select",
        json={"question": question, "account_id": account_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def call_model(question: str, schema: str) -> dict:
    resp = requests.post(
        f"{MODEL_SERVICE_URL}/generate",
        json={"question": question, "schema": schema},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
def health():
    """Vérifie l'état de tous les services."""
    services = {
        "model":   f"{MODEL_SERVICE_URL}/health",
        "rewrite": f"{REWRITE_SERVICE_URL}/health",
        "schema":  f"{SCHEMA_SERVICE_URL}/health",
    }
    statuses = {}
    for name, url in services.items():
        try:
            r = requests.get(url, timeout=5)
            statuses[name] = r.json()
        except Exception as exc:
            statuses[name] = {"error": str(exc)}

    all_ok = all(
        s.get("status") == "ok"
        for s in statuses.values()
        if isinstance(s, dict)
    )

    return jsonify({
        "api":      "ok",
        "services": statuses,
        "ready":    all_ok,
    })


@app.get("/api/v1/schemas")
def list_schemas():
    """Liste les comptes disponibles depuis schema_service."""
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            host=os.getenv("NL2SQL_DB_HOST",     "postgres_nl2sql"),
            port=int(os.getenv("NL2SQL_DB_PORT",  "5432")),
            dbname=os.getenv("NL2SQL_DB_NAME",    "nl2sql"),
            user=os.getenv("NL2SQL_DB_USER",      "nl2sql"),
            password=os.getenv("NL2SQL_DB_PASSWORD", "nl2sql"),
        )
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, pg_db FROM nl2sql_accounts ORDER BY id")
            accounts = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"accounts": accounts})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/v1/index")
def index():
    """
    Indexe les tables d'un compte depuis sa BDD source.
    Body : { "account_id": 1 }
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/index",
            json={"account_id": account_id},
            timeout=300,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout — indexation trop longue"}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"Erreur schema_service : {exc}"}), 502


@app.post("/api/v1/annotate/table")
def annotate_table():
    """Proxy vers schema_service /annotate/table"""
    data = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/annotate/table",
            json=data, timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/v1/annotate/column")
def annotate_column():
    """Proxy vers schema_service /annotate/column"""
    data = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/annotate/column",
            json=data, timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/v1/translate")
def translate():
    """
    Pipeline complet NL → SQL.

    Body : {
      "question":   "Quel est le panier moyen par client ?",
      "account_id": 1
    }

    Retourne : {
      "question_original":  "...",
      "question_rewritten": "...",
      "tables_selected":    [...],
      "columns_display":    [...],
      "sql":                "SELECT ...",
      "elapsed_s":          28.3
    }
    """
    import time
    t0   = time.time()
    data = request.get_json(silent=True) or {}

    question   = data.get("question", "").strip()
    account_id = data.get("account_id")

    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    logger.info("Pipeline démarré — question: %s", question[:80])

    # ── Étape 1 : rewrite ──────────────────────────────────────────────────
    try:
        rewrite_result      = call_rewrite(question)
        question_rewritten  = rewrite_result.get("question_rewritten", question)
        logger.info("Rewrite OK : '%s'", question_rewritten[:80])
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le rewrite_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout rewrite_service"}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"Erreur rewrite_service : {exc}"}), 502

    # ── Étape 2 : schema ───────────────────────────────────────────────────
    try:
        schema_result   = call_schema(question_rewritten, account_id)
        schema_ddl      = schema_result.get("schema_ddl", "")
        tables_selected = schema_result.get("tables", [])
        columns_display = schema_result.get("columns_display", [])
        logger.info("Schema OK : %d tables", len(tables_selected))
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"Erreur schema_service : {exc}"}), 502

    if not schema_ddl:
        return jsonify({"error": "Schéma vide — lancez /api/v1/index d'abord"}), 400

    # ── Étape 3 : model ────────────────────────────────────────────────────
    try:
        model_result = call_model(question_rewritten, schema_ddl)
        sql          = model_result.get("sql")
        logger.info("Model OK : %s", str(sql)[:80])
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le model_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout — le modèle met trop de temps à répondre"}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"error": f"Erreur model_service : {exc}"}), 502

    elapsed = round(time.time() - t0, 2)
    logger.info("✅ Pipeline terminé en %ss", elapsed)

    return jsonify({
        "question_original":  question,
        "question_rewritten": question_rewritten,
        "tables_selected":    tables_selected,
        "columns_display":    columns_display,
        "sql":                sql,
        "elapsed_s":          elapsed,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
