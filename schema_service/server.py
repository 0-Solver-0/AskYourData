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
  POST /select/structured       — question découpée (sujet, mesures, filtres…) → schéma minimal
  POST /annotate/table          — annoter une table (prioritaire sur l'automatique)
  POST /annotate/column         — annoter une colonne (prioritaire sur l'automatique)
  POST /reindex                 — recalcule les embeddings d'une table et de ses colonnes
  GET  /accounts                — comptes (bases sources) connus
  GET  /modules                 — modules Odoo de l'index
  GET  /export                  — fichier d'enrichissement d'un module (JSON)
  POST /import                  — applique un fichier d'enrichissement, réencode les tables modifiées
"""

import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request
from pgvector.psycopg2 import register_vector

from . import search, structured
from .accounts import default_account_from_env, sync_default_account
from .embedder import Embedder, EmbedderConfig
from .enrichment import export_schema, import_enrichment, list_modules
from .filters import load_filter_config
from .indexer import index_account, index_report, reindex_table_embeddings
from .search_data import column_similarities, load_search_data, vector_ranking
from .vector_space import ensure_vector_space, missing_embeddings

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("schema_service")

# ── Config ─────────────────────────────────────────────────────────────────
NL2SQL_DB    = {
    "host":     os.getenv("NL2SQL_DB_HOST",     "postgres_nl2sql"),
    "port":     int(os.getenv("NL2SQL_DB_PORT",  "5432")),
    "dbname":   os.getenv("NL2SQL_DB_NAME",      "nl2sql"),
    "user":     os.getenv("NL2SQL_DB_USER",      "nl2sql"),
    "password": os.getenv("NL2SQL_DB_PASSWORD",  "nl2sql"),
}
SCHEMA_SQL    = os.path.join(os.path.dirname(__file__), "schema.sql")

app      = Flask(__name__)
app.json.sort_keys = False   # garder l'ordre des champs (fichiers d'enrichissement lisibles)
embedder: Embedder = None
vector_state: dict = {}


# ── Embedder ───────────────────────────────────────────────────────────────
def load_embedder():
    """Modèle, dimension, appareil et consignes : voir schema_service/embedder.py (EMBEDDER_*)."""
    global embedder
    embedder = Embedder(EmbedderConfig.from_env()).load()


def embed(texts: list[str]) -> list[list[float]]:
    """Textes indexés (tables, colonnes) : sans consigne."""
    return embedder.embed_documents(texts)


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

    reindex_required = None
    if db_ok is True:
        try:
            conn = get_conn()
            reindex_required = missing_embeddings(conn) > 0
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    return jsonify({
        "status":           "ok" if db_ok is True and embedder is not None else "error",
        "embedder":         embedder.info() if embedder is not None else "not_loaded",
        "reindex_required": reindex_required,
        "vector_space":     vector_state,
        "db_connected":     db_ok,
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
        params   = plan_params()
        query    = search.build_query(question, search.load_synonyms())
        q_vector = embedder.embed_query(query.expanded_text(), "tables")   # question + synonymes

        conn = get_conn()
        try:
            candidates, tables_by_name, columns_by_table, relations, values = \
                load_search_data(conn, account_id, q_vector)
            if not candidates:
                return jsonify({"error": "Aucune table indexée — lancez /index d'abord"}), 404
            vec_ranking = None
            if debug:
                # Mesure : la question telle quelle, comparée aux seuls vecteurs des tables
                vec_ranking = vector_ranking(conn, account_id, embedder.embed_query(question, "tables"))
        finally:
            conn.close()

        value_matches = search.match_values(question, values)
        plan = search.plan_schema(query, candidates, tables_by_name, columns_by_table, relations, params,
                                  value_matches)
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
                "embedder": embedder.cfg.model_id,
                "tables": plan["tables"],
                "ranking": plan["ranking"],
                "vector_ranking": vec_ranking,
                "value_matches": plan["value_matches"],
            }
        return jsonify(response)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /select")
        return jsonify({"error": str(exc)}), 500


def structured_params() -> structured.StructuredParams:
    return structured.StructuredParams(
        candidate_tables=int(os.getenv("STRUCTURED_CANDIDATE_TABLES", "6")),
        subject_tables=int(os.getenv("STRUCTURED_SUBJECT_TABLES", "1")),
        matches_per_piece=int(os.getenv("STRUCTURED_MATCHES_PER_PIECE", "2")),
        subject_margin=float(os.getenv("STRUCTURED_SUBJECT_MARGIN", "0.06")),
        check_types=os.getenv("STRUCTURED_CHECK_TYPES", "true").lower() not in ("0", "false", "no"),
        min_similarity=float(os.getenv("STRUCTURED_MIN_SIMILARITY", "0.6")),
        table_piece_min=float(os.getenv("STRUCTURED_TABLE_PIECE_MIN", "0.6")),
        token_budget=int(os.getenv("SCHEMA_TOKEN_BUDGET", "3000")),
    )


@app.post("/select/structured")
def select_structured():
    """
    Question découpée en morceaux de sens → tables et colonnes → schéma minimal.
    Body : {"account_id": 1, "question": "… (facultatif)", "debug": false,
            "structured": {"subject": "...", "measures": [...], "filters": [...],
                           "group_by": [...], "period": "...", "order": "..."}}
    Le sujet est cherché parmi les fiches de tables (consigne « tables »), chaque autre morceau
    parmi les colonnes des tables candidates (consigne « columns »).
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    question   = (data.get("question") or "").strip()
    debug      = bool(data.get("debug", False))
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        sq = structured.StructuredQuestion.from_dict(data.get("structured"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        t0     = time.time()
        params = structured_params()
        pieces = sq.pieces()
        subject_vec = embedder.embed_query(sq.subject, "tables")
        piece_vecs  = embedder.embed_queries([text for _, text in pieces], "columns")
        # Regroupements comparés aussi aux fiches de tables (« product category » → product_category)
        group_idx = [i for i, (role, _) in enumerate(pieces) if role == "group_by"]
        group_vecs = embedder.embed_queries([pieces[i][1] for i in group_idx], "tables") if group_idx else []

        conn = get_conn()
        try:
            candidates, tables_by_name, columns_by_table, relations, values = \
                load_search_data(conn, account_id, subject_vec)
            if not candidates:
                return jsonify({"error": "Aucune table indexée — lancez /index d'abord"}), 404
            table_sims = {c.table_name: c.vector_score for c in candidates}
            names = sorted(table_sims, key=table_sims.get, reverse=True)[:params.candidate_tables]
            column_sims = column_similarities(conn, account_id, names, piece_vecs)
            piece_table_sims: list[dict[str, float]] = [{} for _ in pieces]
            for i, vec in zip(group_idx, group_vecs):
                piece_table_sims[i] = {r["table_name"]: r["similarity"]
                                       for r in vector_ranking(conn, account_id, vec, limit=5)}
        finally:
            conn.close()

        value_text = question or " ".join([sq.subject, *sq.filters])
        value_matches = search.match_values(value_text, values)
        plan = structured.plan_structured(sq, table_sims, column_sims, tables_by_name, columns_by_table,
                                          relations, params, value_matches, piece_table_sims)
        elapsed = round(time.time() - t0, 3)
        logger.info("✅ Select structuré en %ss | %d tables | ~%d tokens",
                    elapsed, len(plan["tables"]), plan["token_estimate"])

        response = {
            "tables": [{"table_name": t["table_name"], "reason": t["reason"]} for t in plan["tables"]],
            "columns_display": [f"{t['table_name']}.{c['column_name']}" for t in plan["tables"]
                                for c in t["columns"] if c["reason"] not in ("primary_key", "join")],
            "schema_ddl": plan["schema_ddl"],
            "relations": plan["relations"],
            "token_estimate": plan["token_estimate"],
            "elapsed_s": elapsed,
        }
        if debug:
            response["debug"] = {
                "embedder": embedder.cfg.model_id,
                "structured": sq.__dict__,
                "subject_ranking": plan["subject_ranking"],
                "pieces": plan["pieces"],
                "tables": plan["tables"],
                "value_matches": plan["value_matches"],
                "value_matches_ignored": plan["value_matches_ignored"],
            }
        return jsonify(response)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /select/structured")
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


@app.get("/accounts")
def get_accounts():
    """Comptes (bases sources) connus de l'index : id, nom, base."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, pg_db, pg_schema FROM nl2sql_accounts ORDER BY id")
            accounts = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"accounts": accounts})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /accounts")
        return jsonify({"error": str(exc)}), 500


@app.get("/modules")
def get_modules():
    """Modules Odoo de l'index (tables, tables enrichies). Query : ?account_id=1"""
    account_id = request.args.get("account_id", type=int)
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        conn = get_conn()
        modules = list_modules(conn, account_id)
        conn.close()
        return jsonify({"account_id": account_id, "modules": modules})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /modules")
        return jsonify({"error": str(exc)}), 500


@app.get("/export")
def export():
    """
    Fichier d'enrichissement (JSON) d'un module ou d'une liste de tables.
    Query : ?account_id=1&module=account  |  &tables=account_move,sale_order  |  &include_technical=true
    """
    account_id = request.args.get("account_id", type=int)
    module = (request.args.get("module") or "").strip() or None
    tables = [t.strip() for t in (request.args.get("tables") or "").split(",") if t.strip()] or None
    include_technical = (request.args.get("include_technical") or "").lower() in ("1", "true", "yes")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    if not module and not tables:
        return jsonify({"error": "préciser 'module' ou 'tables' (liste des modules : /modules)"}), 400
    try:
        conn = get_conn()
        data = export_schema(conn, account_id, module, tables, include_technical)
        conn.close()
        if not data["tables"]:
            return jsonify({"error": "aucune table indexée pour ce module ou ces tables"}), 404
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /export")
        return jsonify({"error": str(exc)}), 500


@app.post("/import")
def import_():
    """
    Applique un fichier d'enrichissement (le JSON de /export, rempli).
    Body : le fichier. Query : ?account_id=1 (sinon celui du fichier) &dry_run=true &source=manual
    Les tables modifiées sont réencodées aussitôt (pas de réindexation complète).
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "corps JSON attendu (fichier exporté par /export)"}), 400
    account_id = request.args.get("account_id", type=int) or payload.get("account_id")
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    source = (request.args.get("source") or "manual").strip()[:200]
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400
    try:
        conn = get_conn()
        report = import_enrichment(conn, int(account_id), payload, dry_run=dry_run, source=source)
        t0 = time.time()
        for table_id in report.pop("changed_table_ids"):
            reindex_table_embeddings(conn, table_id, embed)
        report["reencoded_s"] = round(time.time() - t0, 2)
        conn.close()
        logger.info("Import d'enrichissement%s : %d tables, %d champs",
                    " (simulation)" if dry_run else "", len(report["tables_changed"]), report["fields_changed"])
        return jsonify(report)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur /import")
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
_conn = psycopg2.connect(**NL2SQL_DB)
vector_state = ensure_vector_space(_conn, embedder.dimension, embedder.cfg.model_id)
_conn.close()
if vector_state["reindex_required"]:
    logger.warning("⚠️  Vecteurs absents ou obsolètes : relancez POST /index pour ce modèle (%s)",
                   embedder.cfg.model_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=False, threaded=True)
