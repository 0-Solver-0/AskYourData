"""
api_service/app.py
------------------
Orchestrateur du pipeline AskYourData (architecture v3, voir documentation/ARCHITECTURE.md).

Pipeline actuel :
  1. schema_service   — identifie tables, colonnes, relations
  2. génération SQL   — pas encore branchée (rôle LLM `SQL`, incréments 6-7)

Endpoints :
  POST /api/v1/translate   — question → schéma réduit (sql = null tant que la génération n'est pas branchée)
  POST /api/v1/schema/debug — diagnostic de la recherche de schéma
  GET  /api/v1/health      — healthcheck des services
  GET  /api/v1/schemas     — liste les comptes disponibles
  POST /api/v1/index       — indexe les tables d'un compte (dry_run possible)
  GET  /api/v1/index/report — tables retenues / exclues d'un compte
  POST /api/v1/annotate/table   — annoter une table
  POST /api/v1/annotate/column  — annoter une colonne
  GET  /api/v1/schema/modules   — modules Odoo de l'index
  GET  /api/v1/schema/export    — fichier d'enrichissement d'un module (JSON)
  POST /api/v1/schema/import    — applique un fichier d'enrichissement (dry_run possible)
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
app.json.sort_keys = False   # garder l'ordre des champs (fichiers d'enrichissement lisibles)
app.config["SECRET_KEY"] = SECRET_KEY


# ── Helpers ─────────────────────────────────────────────────────────────────

def call_schema(question: str, account_id: int, debug: bool = False) -> dict:
    resp = requests.post(
        f"{SCHEMA_SERVICE_URL}/select",
        json={"question": question, "account_id": account_id, "debug": debug},
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
    Indexe les tables d'un compte depuis sa base source.
    Body : { "account_id": 1, "dry_run": false, "embeddings": true }
      dry_run    : true → seulement la liste des tables retenues / exclues, rien n'est écrit
      embeddings : false → structure seulement (plus rapide)
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    payload = {"account_id": account_id}
    for key in ("dry_run", "embeddings"):
        if key in data:
            payload[key] = bool(data[key])

    try:
        resp = requests.post(f"{SCHEMA_SERVICE_URL}/index", json=payload, timeout=900)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout — indexation trop longue"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


def _proxy(method: str, path: str, timeout: int = 60, **kwargs):
    """Relaye une requête vers schema_service et renvoie sa réponse telle quelle."""
    try:
        resp = requests.request(method, f"{SCHEMA_SERVICE_URL}{path}", timeout=timeout, **kwargs)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


@app.get("/api/v1/schema/modules")
def schema_modules():
    """Modules Odoo de l'index, avec le nombre de tables enrichies. Query : ?account_id=1"""
    return _proxy("GET", "/modules", params=request.args)


@app.get("/api/v1/schema/export")
def schema_export():
    """
    Fichier d'enrichissement d'un module. Query : ?account_id=1&module=account
    (ou &tables=account_move,sale_order ; &include_technical=true pour toutes les colonnes)
    """
    return _proxy("GET", "/export", params=request.args)


@app.post("/api/v1/schema/import")
def schema_import():
    """
    Applique un fichier d'enrichissement rempli. Body : le fichier JSON.
    Query : ?account_id=1&dry_run=true (simulation : liste des changements sans rien écrire)
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "corps JSON attendu (fichier exporté par /api/v1/schema/export)"}), 400
    return _proxy("POST", "/import", timeout=300, params=request.args, json=payload)


@app.get("/api/v1/index/report")
def index_report():
    """Tables retenues et exclues (avec raison) d'un compte. Query : ?account_id=1"""
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        resp = requests.get(f"{SCHEMA_SERVICE_URL}/index/report",
                            params={"account_id": account_id}, timeout=30)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


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


@app.post("/api/v1/schema/debug")
def schema_debug():
    """
    Diagnostic de la recherche de schéma : tables retenues et pourquoi (recherche, jointure,
    référence), détail des scores, colonnes gardées, classement, taille en tokens.
    Body : {"question": "...", "account_id": 1, "search_mode": "legacy" | "coverage" (optionnel)}
    """
    data = request.get_json(silent=True) or {}
    if not data.get("question") or not data.get("account_id"):
        return jsonify({"error": "'question' et 'account_id' obligatoires"}), 400
    try:
        resp = requests.post(
            f"{SCHEMA_SERVICE_URL}/select",
            json={"question": data["question"], "account_id": data["account_id"], "debug": True,
                  "search_mode": data.get("search_mode")},
            timeout=30,
        )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Impossible de joindre le schema_service"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout schema_service"}), 504
    except ValueError:
        return jsonify({"error": "Réponse invalide du schema_service"}), 502


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
        "token_estimate":     schema_result.get("token_estimate"),
        "sql":                None,
        "status":             "generation_not_configured",
        "elapsed_s":          elapsed,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
