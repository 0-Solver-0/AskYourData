"""
api_service/app.py
------------------
Orchestrateur du pipeline AskYourData (architecture v3, voir documentation/ARCHITECTURE.md).

Pipeline actuel :
  1. schema_service   — identifie tables, colonnes, relations
  2. génération SQL   — pas encore branchée (rôle LLM `SQL`, incréments 6-7)

Endpoints :
  POST /api/v1/translate   — question → schéma réduit (sql = null tant que la génération n'est pas branchée)
  GET  /api/v1/health      — healthcheck des services
  GET  /api/v1/schemas     — liste les comptes disponibles
  POST /api/v1/index       — indexe les tables d'un compte
  POST /api/v1/annotate/table   — annoter une table
  POST /api/v1/annotate/column  — annoter une colonne
"""

import logging
import os

import requests
from flask import Flask, jsonify, request

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("api_service")

# ── Config ─────────────────────────────────────────────────────────────────
SCHEMA_SERVICE_URL  = os.getenv("SCHEMA_SERVICE_URL",  "http://schema_service:8002")
SECRET_KEY          = os.getenv("SECRET_KEY",          "dev-secret")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


# ── Helpers ─────────────────────────────────────────────────────────────────

def call_schema(question: str, account_id: int) -> dict:
    resp = requests.post(
        f"{SCHEMA_SERVICE_URL}/select",
        json={"question": question, "account_id": account_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
def health():
    """Vérifie l'état de tous les services."""
    services = {
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
    Question → schéma réduit. La génération SQL n'est pas encore branchée :
    `sql` vaut null et `status` vaut "generation_not_configured".

    Body : {
      "question":   "What is the average order value per customer?",
      "account_id": 1
    }

    Retourne : {
      "question_original":  "...",
      "question_rewritten": "...",   (identique à la question : plus de traduction)
      "tables_selected":    [...],
      "columns_display":    [...],
      "schema_ddl":         "CREATE TABLE ...",
      "sql":                null,
      "status":             "generation_not_configured",
      "elapsed_s":          0.1
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

    # Plus de traduction : les questions sont posées en anglais (décision D1).
    question_rewritten = question

    # ── Étape 1 : schema ───────────────────────────────────────────────────
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

    # ── Étape 2 : génération SQL — à brancher (rôle LLM `SQL`) ─────────────

    elapsed = round(time.time() - t0, 2)
    logger.info("✅ Pipeline terminé en %ss", elapsed)

    return jsonify({
        "question_original":  question,
        "question_rewritten": question_rewritten,
        "tables_selected":    tables_selected,
        "columns_display":    columns_display,
        "schema_ddl":         schema_ddl,
        "sql":                None,
        "status":             "generation_not_configured",
        "elapsed_s":          elapsed,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
