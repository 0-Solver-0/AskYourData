"""
schema_service/server.py
------------------------
Indexation du schéma de la base source et recherche des tables, colonnes et
relations utiles à une question (embedder local + pgvector).

Endpoints :
  GET  /health                  — état du service
  POST /index                   — indexe les tables d'un compte (filtrage, métadonnées Odoo, embeddings)
  GET  /index/report            — tables retenues et exclues (avec raison) pour relecture
  POST /select                  — question → tables (recherche, jointures, références) → schéma compact
  POST /annotate/table          — annoter une table (prioritaire sur l'automatique)
  POST /annotate/column         — annoter une colonne (prioritaire sur l'automatique)
  POST /reindex                 — recalcule les embeddings d'une table et de ses colonnes
"""

import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer

from . import search
from .accounts import default_account_from_env, sync_default_account
from .filters import load_filter_config
from .indexer import index_account, index_report, reindex_table_embeddings

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("schema_service")

# ── Config ─────────────────────────────────────────────────────────────────
MODEL_PATH   = os.getenv("MINILM_MODEL_PATH", "/app/models/all-MiniLM-L6-v2")
NL2SQL_DB    = {
    "host":     os.getenv("NL2SQL_DB_HOST",     "postgres_nl2sql"),
    "port":     int(os.getenv("NL2SQL_DB_PORT",  "5432")),
    "dbname":   os.getenv("NL2SQL_DB_NAME",      "nl2sql"),
    "user":     os.getenv("NL2SQL_DB_USER",      "nl2sql"),
    "password": os.getenv("NL2SQL_DB_PASSWORD",  "nl2sql"),
}
SCHEMA_SQL    = os.path.join(os.path.dirname(__file__), "schema.sql")

app      = Flask(__name__)
embedder = None


# ── Embedder ───────────────────────────────────────────────────────────────
def load_embedder():
    global embedder
    logger.info("⏳ Chargement de l'embedder (%s)…", MODEL_PATH)
    embedder = SentenceTransformer(MODEL_PATH)
    logger.info("✅ Embedder OK")


def embed(texts: list[str]) -> list[list[float]]:
    return embedder.encode(texts, batch_size=64).tolist()


def embed_one(text: str) -> list[float]:
    return embedder.encode(text).tolist()


# ── DB helpers ─────────────────────────────────────────────────────────────
def get_conn():
    conn = psycopg2.connect(**NL2SQL_DB)
    register_vector(conn)
    return conn


def ensure_schema(retries: int = 10, delay_s: float = 3.0) -> None:
    """Applique schema_service/schema.sql (idempotent) : crée ou met à niveau la base NL2SQL."""
    with open(SCHEMA_SQL, encoding="utf-8") as fh:
        ddl = fh.read()
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(**NL2SQL_DB)
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            conn.close()
            logger.info("✅ Schéma NL2SQL à jour")
            return
        except psycopg2.OperationalError as exc:
            logger.warning("Base NL2SQL indisponible (essai %d/%d) : %s", attempt, retries, exc)
            time.sleep(delay_s)
    raise RuntimeError("Impossible d'appliquer le schéma NL2SQL")


def get_account(conn, account_id: int) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM nl2sql_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Compte {account_id} introuvable")
        return dict(row)


def get_source_conn(account: dict):
    """Connexion vers la base source (ex. Odoo)."""
    return psycopg2.connect(
        host=account["pg_host"],
        port=account["pg_port"],
        dbname=account["pg_db"],
        user=account["pg_user"],
        password=os.getenv("SOURCE_DB_PASSWORD", "odoo"),
    )


# ── Recherche de schéma ────────────────────────────────────────────────────
def load_search_data(conn, account_id: int, q_vector: list[float]):
    """
    Charge en une fois, pour un compte : tables (avec similarité à la question),
    colonnes (avec similarité) et relations. Quelques centaines de tables : quelques ms.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, table_name, odoo_model, label, odoo_label, synonyms, domain, description, row_count,
                   CASE WHEN embedding IS NULL THEN 0 ELSE 1 - (embedding <=> %s::vector) END AS similarity
            FROM nl2sql_tables WHERE account_id = %s
        """, (q_vector, account_id))
        tables = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT t.table_name, c.column_name, c.data_type, c.is_pk, c.is_fk, c.is_nullable,
                   c.label, c.synonyms, c.display, c.odoo_label, c.odoo_type, c.selection_values,
                   CASE WHEN c.embedding IS NULL THEN 0 ELSE 1 - (c.embedding <=> %s::vector) END AS similarity
            FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
            WHERE t.account_id = %s
            ORDER BY t.table_name, c.id
        """, (q_vector, account_id))
        columns_by_table: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            columns_by_table.setdefault(r["table_name"], []).append(dict(r))
        cur.execute("""
            SELECT from_table, from_column, to_table, to_column, label, join_hint
            FROM nl2sql_relations WHERE account_id = %s
        """, (account_id,))
        relations = [dict(r) for r in cur.fetchall()]

    candidates = []
    for t in tables:
        cols = columns_by_table.get(t["table_name"], [])
        candidates.append(search.TableCandidate(
            id=t["id"],
            table_name=t["table_name"],
            row_count=t["row_count"],
            core_tokens=search.table_core_tokens(t),
            column_tokens=search.column_tokens(cols),
            vector_score=float(t["similarity"] or 0),
            column_vector_score=max((float(c["similarity"] or 0) for c in cols
                                     if c["column_name"] not in search.NOISE_COLUMNS), default=0.0),
        ))
    tables_by_name = {t["table_name"]: t for t in tables}
    return candidates, tables_by_name, columns_by_table, relations


def plan_params() -> search.PlanParams:
    return search.PlanParams(
        seed_tables=int(os.getenv("SEARCH_SEED_TABLES", "5")),
        max_tables=int(os.getenv("SEARCH_MAX_TABLES", "10")),
        max_columns=int(os.getenv("SEARCH_MAX_COLUMNS", "15")),
        token_budget=int(os.getenv("SCHEMA_TOKEN_BUDGET", "3000")),
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_ok = str(e)

    return jsonify({
        "status":       "ok" if db_ok is True and embedder is not None else "error",
        "embedder":     "ok" if embedder is not None else "not_loaded",
        "model_path":   MODEL_PATH,
        "db_connected": db_ok,
    }), 200 if db_ok is True else 500


@app.post("/select")
def select():
    """
    Question → tables et colonnes utiles → schéma compact.

    Body : {"question": "...", "account_id": 1, "debug": false}
    Retourne : {"tables", "columns_display", "schema_ddl", "relations", "token_estimate", "elapsed_s"}
      tables : [{table_name, reason (search | join:a↔b | lookup:table.colonne), score, …}]
      debug  : true → ajoute le détail des scores, des colonnes gardées et le classement
    """
    data       = request.get_json(silent=True) or {}
    question   = data.get("question", "").strip()
    account_id = data.get("account_id")
    debug      = bool(data.get("debug", False))

    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    try:
        t0       = time.time()
        query    = search.build_query(question, search.load_synonyms())
        q_vector = embed_one(query.expanded_text())   # une seule fois par question

        conn = get_conn()
        candidates, tables_by_name, columns_by_table, relations = load_search_data(conn, account_id, q_vector)
        conn.close()
        if not candidates:
            return jsonify({"error": "Aucune table indexée — lancez /index d'abord"}), 404

        plan = search.plan_schema(query, candidates, tables_by_name, columns_by_table, relations, plan_params())
        elapsed = round(time.time() - t0, 3)
        logger.info("✅ Select en %ss | %d tables | ~%d tokens",
                    elapsed, len(plan["tables"]), plan["token_estimate"])

        response = {
            "tables": [{"table_name": t["table_name"], "reason": t["reason"], "score": t["score"]}
                       for t in plan["tables"]],
            "columns_display": [f"{t['table_name']}.{c['column_name']}"
                                for t in plan["tables"] for c in t["columns"]
                                if c["reason"] not in ("primary_key", "join")],
            "schema_ddl": plan["schema_ddl"],
            "relations": plan["relations"],
            "token_estimate": plan["token_estimate"],
            "elapsed_s": elapsed,
        }
        if debug:
            response["debug"] = {
                "query_terms": {t: sorted(v) for t, v in query.expansions.items()},
                "encoded_text": query.expanded_text(),
                "tables": plan["tables"],
                "ranking": plan["ranking"],
            }
        return jsonify(response)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /select")
        return jsonify({"error": str(exc)}), 500


@app.post("/index")
def index():
    """
    Indexe les tables de la base source d'un compte.

    Body : {
      "account_id": 1,
      "dry_run":    false,   // true : calcule seulement les tables retenues/exclues, sans rien écrire
      "embeddings": true     // false : structure seulement (plus rapide), embeddings plus tard via /reindex
    }
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    try:
        cfg     = load_filter_config()
        conn    = get_conn()
        account = get_account(conn, account_id)
        src     = get_source_conn(account)
        try:
            report = index_account(
                conn, src, account,
                embed=embed if data.get("embeddings", True) else None,
                cfg=cfg,
                dry_run=bool(data.get("dry_run", False)),
            )
        finally:
            src.close()
            conn.close()
        return jsonify(report)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /index")
        return jsonify({"error": str(exc)}), 500


@app.get("/index/report")
def get_index_report():
    """Tables retenues (volume, colonnes, embedding) et tables exclues (raison). Query : ?account_id=1"""
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        conn = get_conn()
        report = index_report(conn, account_id)
        conn.close()
        return jsonify(report)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /index/report")
        return jsonify({"error": str(exc)}), 500


@app.post("/annotate/table")
def annotate_table():
    """
    Body : {"account_id": 1, "table_name": "sale_order", "label": "...", "synonyms": "...",
            "domain": "...", "notes": "...", "description": "..."}
    Les annotations manuelles sont prioritaires : l'indexation et l'enrichissement ne les écrasent pas.
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    table_name = data.get("table_name", "").strip()

    if not account_id or not table_name:
        return jsonify({"error": "'account_id' et 'table_name' obligatoires"}), 400

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE nl2sql_tables
                SET label           = COALESCE(%s, label),
                    synonyms        = COALESCE(%s, synonyms),
                    domain          = COALESCE(%s, domain),
                    notes           = COALESCE(%s, notes),
                    description     = COALESCE(%s, description),
                    manual_override = TRUE
                WHERE account_id = %s AND table_name = %s
                RETURNING id
            """, (
                data.get("label"), data.get("synonyms"), data.get("domain"),
                data.get("notes"), data.get("description"),
                account_id, table_name,
            ))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"Table '{table_name}' introuvable"}), 404
            table_id = row[0]
        conn.commit()

        reindex_table_embeddings(conn, table_id, embed)
        conn.close()
        return jsonify({"status": "ok", "table_name": table_name, "reindexed": True})

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /annotate/table")
        return jsonify({"error": str(exc)}), 500


@app.post("/annotate/column")
def annotate_column():
    """
    Body : {"account_id": 1, "table_name": "sale_order", "column_name": "amount_total",
            "label": "...", "synonyms": "...", "example": "...", "notes": "...", "display": true}
    """
    data        = request.get_json(silent=True) or {}
    account_id  = data.get("account_id")
    table_name  = data.get("table_name",  "").strip()
    column_name = data.get("column_name", "").strip()

    if not account_id or not table_name or not column_name:
        return jsonify({"error": "'account_id', 'table_name', 'column_name' obligatoires"}), 400

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE nl2sql_columns c
                SET label           = COALESCE(%s, c.label),
                    synonyms        = COALESCE(%s, c.synonyms),
                    example         = COALESCE(%s, c.example),
                    notes           = COALESCE(%s, c.notes),
                    display         = COALESCE(%s, c.display),
                    manual_override = TRUE
                FROM nl2sql_tables t
                WHERE c.table_id    = t.id
                  AND t.account_id  = %s
                  AND t.table_name  = %s
                  AND c.column_name = %s
                RETURNING t.id
            """, (
                data.get("label"), data.get("synonyms"),
                data.get("example"), data.get("notes"),
                data.get("display"),
                account_id, table_name, column_name,
            ))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"Colonne '{column_name}' introuvable"}), 404
            table_id = row[0]
        conn.commit()

        reindex_table_embeddings(conn, table_id, embed)
        conn.close()
        return jsonify({"status": "ok", "column_name": column_name, "reindexed": True})

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /annotate/column")
        return jsonify({"error": str(exc)}), 500


@app.post("/reindex")
def reindex():
    """
    Recalcule les embeddings d'une table et de ses colonnes.
    Body : {"account_id": 1, "table_name": "sale_order"}
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    table_name = data.get("table_name", "").strip()

    if not account_id or not table_name:
        return jsonify({"error": "'account_id' et 'table_name' obligatoires"}), 400

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM nl2sql_tables
                WHERE account_id = %s AND table_name = %s
            """, (account_id, table_name))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"Table '{table_name}' introuvable"}), 404
            table_id = row[0]

        reindex_table_embeddings(conn, table_id, embed)
        conn.close()
        return jsonify({"status": "ok", "table_name": table_name, "reindexed": True})

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /reindex")
        return jsonify({"error": str(exc)}), 500


# ── Démarrage ──────────────────────────────────────────────────────────────
ensure_schema()
_conn = psycopg2.connect(**NL2SQL_DB)
_account = sync_default_account(_conn, default_account_from_env())
_conn.close()
logger.info("✅ Compte par défaut : %s@%s:%s/%s (schéma %s)", _account["pg_user"], _account["pg_host"],
            _account["pg_port"], _account["pg_db"], _account["pg_schema"])
load_embedder()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=False, threaded=True)
