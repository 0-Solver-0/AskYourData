"""
schema_service/server.py
------------------------
Agent 2 — Identifier les tables
Agent 3 — Identifier les colonnes
Agent 4 — Identifier les relations

Utilise all-MiniLM-L6-v2 pour les embeddings et pgvector pour la recherche.

Endpoints :
  GET  /health                  — état du service
  POST /index                   — indexe toutes les tables d'un compte
  POST /select                  — pipeline complet : tables → colonnes → relations → DDL minimal
  POST /annotate/table          — annoter une table
  POST /annotate/column         — annoter une colonne
  POST /reindex                 — recalcule l'embedding d'une table
"""

import os
import sys
import logging
import time

import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer
from flask import Flask, request, jsonify

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
TOP_K_TABLES  = int(os.getenv("TOP_K_TABLES",  "10"))
TOP_K_COLUMNS = int(os.getenv("TOP_K_COLUMNS", "20"))

app     = Flask(__name__)
embedder = None


# ── Chargement embedder ────────────────────────────────────────────────────
def load_embedder():
    global embedder
    logger.info("⏳ Chargement all-MiniLM-L6-v2…")
    embedder = SentenceTransformer(MODEL_PATH)
    logger.info("✅ Embedder OK")

load_embedder()


# ── DB helpers ─────────────────────────────────────────────────────────────
def get_conn():
    conn = psycopg2.connect(**NL2SQL_DB)
    register_vector(conn)
    return conn

def get_account(conn, account_id: int) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM nl2sql_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Compte {account_id} introuvable")
        return dict(row)

def get_source_conn(account: dict):
    """Connexion vers la BDD source (ex: Odoo)."""
    return psycopg2.connect(
        host=account["pg_host"],
        port=account["pg_port"],
        dbname=account["pg_db"],
        user=account["pg_user"],
        password=os.getenv("SOURCE_DB_PASSWORD", "odoo"),
    )


# ── Construction du texte d'embedding ─────────────────────────────────────
def build_embedding_text(table: dict, columns: list, relations: list) -> str:
    """
    Fusionne table + colonnes + relations en un seul texte
    pour maximiser la précision de la recherche sémantique.
    """
    parts = []

    # Table
    parts.append(table["table_name"])
    if table.get("label"):
        parts.append(table["label"])
    if table.get("synonyms"):
        parts.append(table["synonyms"])
    if table.get("domain"):
        parts.append(table["domain"])
    if table.get("description"):
        parts.append(table["description"])

    # Colonnes
    col_parts = []
    for col in columns:
        col_text = f"{col['column_name']} {col.get('data_type', '')}"
        extras = []
        if col.get("label"):
            extras.append(col["label"])
        if col.get("synonyms"):
            extras.append(col["synonyms"])
        if extras:
            col_text += f" ({', '.join(extras)})"
        col_parts.append(col_text)
    if col_parts:
        parts.append(" | ".join(col_parts))

    # Relations
    for rel in relations:
        if rel.get("label"):
            parts.append(rel["label"])

    return " | ".join(parts)


# ── Agent 2 — Sélection tables ─────────────────────────────────────────────
def select_tables(conn, question: str, account_id: int) -> list:
    """Recherche sémantique des tables les plus pertinentes."""
    q_vector = embedder.encode(question).tolist()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                id,
                table_name,
                schema_name,
                ddl,
                label,
                synonyms,
                domain,
                1 - (embedding <=> %s::vector) AS similarity
            FROM nl2sql_tables
            WHERE account_id = %s
              AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (q_vector, account_id, q_vector, TOP_K_TABLES))
        return [dict(r) for r in cur.fetchall()]


# ── Agent 3 — Sélection colonnes ───────────────────────────────────────────
def select_columns(conn, question: str, table_ids: list) -> dict:
    """
    Pour chaque table sélectionnée, retourne les colonnes pertinentes.
    Garde toujours : PK, FK, et colonnes sémantiquement proches.
    """
    if not table_ids:
        return {}

    q_vector = embedder.encode(question).tolist()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Récupère toutes les colonnes des tables sélectionnées
        cur.execute("""
            SELECT
                c.id,
                c.table_id,
                c.column_name,
                c.data_type,
                c.is_pk,
                c.is_fk,
                c.is_nullable,
                c.label,
                c.synonyms,
                c.display
            FROM nl2sql_columns c
            WHERE c.table_id = ANY(%s)
            ORDER BY c.table_id, c.is_pk DESC, c.is_fk DESC
        """, (table_ids,))
        all_cols = [dict(r) for r in cur.fetchall()]

    # Grouper par table
    by_table = {}
    for col in all_cols:
        tid = col["table_id"]
        by_table.setdefault(tid, []).append(col)

    # Pour chaque table : garder PK/FK + colonnes pertinentes via similarité texte
    result = {}
    for tid, cols in by_table.items():
        selected = []
        for col in cols:
            # Toujours garder PK et FK (nécessaires pour les jointures)
            if col["is_pk"] or col["is_fk"]:
                selected.append(col)
                continue
            # Similarité sémantique via texte colonne
            col_text = f"{col['column_name']} {col.get('label', '')} {col.get('synonyms', '')}"
            col_vec  = embedder.encode(col_text).tolist()
            q_vec    = embedder.encode(question).tolist()
            # Produit scalaire simplifié
            similarity = sum(a * b for a, b in zip(q_vec, col_vec))
            col["_similarity"] = similarity
            selected.append(col)

        # Trier par pertinence, garder les TOP_K_COLUMNS
        selected = sorted(selected, key=lambda c: (
            c.get("is_pk", False),
            c.get("is_fk", False),
            c.get("_similarity", 0)
        ), reverse=True)[:TOP_K_COLUMNS]

        result[tid] = selected

    return result


# ── Agent 4 — Sélection relations ─────────────────────────────────────────
def select_relations(conn, account_id: int, table_names: list) -> list:
    """Retourne les relations entre les tables sélectionnées."""
    if not table_names:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT from_table, from_column, to_table, to_column, label, join_hint
            FROM nl2sql_relations
            WHERE account_id = %s
              AND from_table = ANY(%s)
              AND to_table   = ANY(%s)
        """, (account_id, table_names, table_names))
        return [dict(r) for r in cur.fetchall()]


# ── Assembleur de DDL minimal ──────────────────────────────────────────────
def build_minimal_ddl(tables: list, columns_by_table: dict, relations: list) -> str:
    """
    Construit un DDL minimal avec uniquement les tables, colonnes
    et relations identifiées par les 3 agents.
    """
    ddl_parts = []

    for table in tables:
        tid  = table["id"]
        cols = columns_by_table.get(tid, [])

        lines = [f"CREATE TABLE {table['table_name']} ("]
        col_lines = []
        for col in cols:
            line = f"    {col['column_name']} {col['data_type']}"
            if col.get("is_pk"):
                line += " PRIMARY KEY"
            if not col.get("is_nullable", True):
                line += " NOT NULL"
            # Ajouter commentaire métier si annoté
            if col.get("label"):
                line += f"  -- {col['label']}"
                if col.get("synonyms"):
                    line += f" ({col['synonyms']})"
            col_lines.append(line)

        # Ajouter FK depuis les relations
        for rel in relations:
            if rel["from_table"] == table["table_name"]:
                fk = (f"    FOREIGN KEY ({rel['from_column']}) "
                      f"REFERENCES {rel['to_table']}({rel['to_column']})")
                col_lines.append(fk)

        lines.append(",\n".join(col_lines))
        lines.append(");")
        ddl_parts.append("\n".join(lines))

    return "\n\n".join(ddl_parts)


# ── Colonnes à afficher ────────────────────────────────────────────────────
def get_display_columns(columns_by_table: dict) -> list:
    """
    Retourne la liste des colonnes à afficher dans les résultats.
    Exclut les colonnes techniques (id, created_at, etc.) sauf si annotées display=True.
    """
    TECHNICAL = {"id", "create_date", "write_date", "create_uid", "write_uid",
                 "created_at", "updated_at", "deleted_at", "__last_update"}
    display = []
    for cols in columns_by_table.values():
        for col in cols:
            name = col["column_name"].lower()
            # Afficher si : explicitement marqué display=True
            # ou si ce n'est pas une colonne technique et pas une PK sans label
            if col.get("display") is True:
                display.append(col["column_name"])
            elif name not in TECHNICAL and not (col.get("is_pk") and not col.get("label")):
                display.append(col["column_name"])
    return display


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        db_ok = True
    except Exception as e:
        db_ok = str(e)

    return jsonify({
        "status":       "ok" if db_ok is True else "error",
        "embedder":     "ok",
        "model_path":   MODEL_PATH,
        "db_connected": db_ok,
    }), 200 if db_ok is True else 500


@app.post("/select")
def select():
    """
    Pipeline complet : tables → colonnes → relations → DDL minimal

    Body : {
      "question":   "What is the average order value per customer?",
      "account_id": 1
    }
    Retourne : {
      "tables":          [...],
      "columns_display": [...],
      "schema_ddl":      "CREATE TABLE ...",
      "relations":       [...],
      "elapsed_s":       0.05
    }
    """
    data       = request.get_json(silent=True) or {}
    question   = data.get("question", "").strip()
    account_id = data.get("account_id")

    if not question:
        return jsonify({"error": "'question' obligatoire"}), 400
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    try:
        t0   = time.time()
        conn = get_conn()

        # Agent 2 — tables
        tables = select_tables(conn, question, account_id)
        if not tables:
            return jsonify({"error": "Aucune table trouvée — lancez /index d'abord"}), 404

        table_ids   = [t["id"]         for t in tables]
        table_names = [t["table_name"] for t in tables]

        # Agent 3 — colonnes
        columns_by_table = select_columns(conn, question, table_ids)

        # Agent 4 — relations
        relations = select_relations(conn, account_id, table_names)

        # Assemblage DDL minimal
        schema_ddl = build_minimal_ddl(tables, columns_by_table, relations)

        # Colonnes à afficher
        display_columns = get_display_columns(columns_by_table)

        conn.close()
        elapsed = round(time.time() - t0, 3)

        logger.info("✅ Select en %ss | %d tables | %d relations",
                    elapsed, len(tables), len(relations))

        return jsonify({
            "tables":          [{"table_name": t["table_name"],
                                 "similarity": round(t["similarity"], 3)}
                                for t in tables],
            "columns_display": display_columns,
            "schema_ddl":      schema_ddl,
            "relations":       relations,
            "elapsed_s":       elapsed,
        })

    except Exception as exc:
        logger.exception("Erreur /select")
        return jsonify({"error": str(exc)}), 500


@app.post("/index")
def index():
    """
    Indexe toutes les tables de la BDD source d'un compte.

    Body : { "account_id": 1 }
    """
    data       = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "'account_id' obligatoire"}), 400

    try:
        t0      = time.time()
        conn    = get_conn()
        account = get_account(conn, account_id)
        src     = get_source_conn(account)

        schema = account.get("pg_schema", "public")

        # Récupérer toutes les tables de la BDD source
        with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type   = 'BASE TABLE'
                ORDER BY table_name
            """, (schema,))
            source_tables = [r["table_name"] for r in cur.fetchall()]

        indexed = 0
        for table_name in source_tables:
            try:
                # DDL simplifié depuis information_schema
                with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT column_name, data_type, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        ORDER BY ordinal_position
                    """, (schema, table_name))
                    cols = cur.fetchall()

                    # PK
                    cur.execute("""
                        SELECT kcu.column_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                        WHERE tc.table_schema = %s
                          AND tc.table_name   = %s
                          AND tc.constraint_type = 'PRIMARY KEY'
                    """, (schema, table_name))
                    pk_cols = {r["column_name"] for r in cur.fetchall()}

                    # FK
                    cur.execute("""
                        SELECT kcu.column_name,
                               ccu.table_name  AS foreign_table,
                               ccu.column_name AS foreign_column
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                        JOIN information_schema.constraint_column_usage ccu
                          ON tc.constraint_name = ccu.constraint_name
                        WHERE tc.table_schema  = %s
                          AND tc.table_name    = %s
                          AND tc.constraint_type = 'FOREIGN KEY'
                    """, (schema, table_name))
                    fk_rows = cur.fetchall()
                    fk_cols = {r["column_name"] for r in fk_rows}

                # Construire DDL
                ddl_lines = [f"CREATE TABLE {table_name} ("]
                for col in cols:
                    nullable = "" if col["is_nullable"] == "YES" else " NOT NULL"
                    pk       = " PRIMARY KEY" if col["column_name"] in pk_cols else ""
                    ddl_lines.append(
                        f"    {col['column_name']} {col['data_type']}{pk}{nullable},"
                    )
                ddl_lines.append(");")
                ddl = "\n".join(ddl_lines)

                # Upsert table dans nl2sql_tables
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nl2sql_tables
                            (account_id, schema_name, table_name, ddl, description)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, schema_name, table_name)
                        DO UPDATE SET ddl = EXCLUDED.ddl, description = EXCLUDED.description
                        RETURNING id
                    """, (account_id, schema, table_name, ddl, None))
                    table_id = cur.fetchone()[0]

                    # Upsert colonnes
                    for col in cols:
                        cur.execute("""
                            INSERT INTO nl2sql_columns
                                (table_id, column_name, data_type, is_nullable, is_pk, is_fk)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (table_id, column_name)
                            DO UPDATE SET data_type  = EXCLUDED.data_type,
                                          is_nullable = EXCLUDED.is_nullable,
                                          is_pk       = EXCLUDED.is_pk,
                                          is_fk       = EXCLUDED.is_fk
                        """, (
                            table_id,
                            col["column_name"],
                            col["data_type"],
                            col["is_nullable"] == "YES",
                            col["column_name"] in pk_cols,
                            col["column_name"] in fk_cols,
                        ))

                    # Upsert relations
                    for fk in fk_rows:
                        cur.execute("""
                            INSERT INTO nl2sql_relations
                                (account_id, from_table, from_column, to_table, to_column)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (account_id, table_name,
                              fk["column_name"], fk["foreign_table"], fk["foreign_column"]))

                conn.commit()

                # Calcul embedding
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT c.column_name, c.data_type, c.label, c.synonyms
                        FROM nl2sql_columns c WHERE c.table_id = %s
                    """, (table_id,))
                    db_cols = [dict(r) for r in cur.fetchall()]

                table_row = {
                    "table_name": table_name,
                    "label": None, "synonyms": None,
                    "domain": None, "description": None,
                }
                emb_text = build_embedding_text(table_row, db_cols, [])
                embedding = embedder.encode(emb_text).tolist()

                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE nl2sql_tables
                        SET embedding = %s::vector, indexed_at = NOW()
                        WHERE id = %s
                    """, (embedding, table_id))
                conn.commit()
                indexed += 1

            except Exception as e:
                logger.warning("⚠️  Table %s ignorée : %s", table_name, e)
                conn.rollback()
                continue

        src.close()
        conn.close()
        elapsed = round(time.time() - t0, 1)

        logger.info("✅ Indexation terminée : %d/%d tables en %ss",
                    indexed, len(source_tables), elapsed)

        return jsonify({
            "indexed":   indexed,
            "total":     len(source_tables),
            "elapsed_s": elapsed,
        })

    except Exception as exc:
        logger.exception("Erreur /index")
        return jsonify({"error": str(exc)}), 500


@app.post("/annotate/table")
def annotate_table():
    """
    Body : {
      "account_id": 1,
      "table_name": "sale_order",
      "label":      "Commandes clients",
      "synonyms":   "commandes, orders, ventes",
      "domain":     "commercial",
      "notes":      "..."
    }
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
                SET label    = COALESCE(%s, label),
                    synonyms = COALESCE(%s, synonyms),
                    domain   = COALESCE(%s, domain),
                    notes    = COALESCE(%s, notes)
                WHERE account_id = %s AND table_name = %s
                RETURNING id
            """, (
                data.get("label"), data.get("synonyms"),
                data.get("domain"), data.get("notes"),
                account_id, table_name,
            ))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"Table '{table_name}' introuvable"}), 404
            table_id = row[0]
        conn.commit()

        # Recalcule l'embedding avec les nouvelles annotations
        _reindex_table(conn, table_id)
        conn.close()

        return jsonify({"status": "ok", "table_name": table_name, "reindexed": True})

    except Exception as exc:
        logger.exception("Erreur /annotate/table")
        return jsonify({"error": str(exc)}), 500


@app.post("/annotate/column")
def annotate_column():
    """
    Body : {
      "account_id":  1,
      "table_name":  "sale_order",
      "column_name": "amount_total",
      "label":       "Montant total",
      "synonyms":    "total, montant, panier, chiffre d'affaires",
      "example":     "1250.00",
      "display":     true
    }
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
                SET label    = COALESCE(%s, c.label),
                    synonyms = COALESCE(%s, c.synonyms),
                    example  = COALESCE(%s, c.example),
                    notes    = COALESCE(%s, c.notes),
                    display  = COALESCE(%s, c.display)
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

        # Recalcule l'embedding de la table parente
        _reindex_table(conn, table_id)
        conn.close()

        return jsonify({"status": "ok", "column_name": column_name, "reindexed": True})

    except Exception as exc:
        logger.exception("Erreur /annotate/column")
        return jsonify({"error": str(exc)}), 500


@app.post("/reindex")
def reindex():
    """
    Recalcule l'embedding d'une table spécifique.
    Body : { "account_id": 1, "table_name": "sale_order" }
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

        _reindex_table(conn, table_id)
        conn.close()
        return jsonify({"status": "ok", "table_name": table_name, "reindexed": True})

    except Exception as exc:
        logger.exception("Erreur /reindex")
        return jsonify({"error": str(exc)}), 500


def _reindex_table(conn, table_id: int):
    """Recalcule et sauvegarde l'embedding d'une table."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM nl2sql_tables WHERE id = %s", (table_id,))
        table = dict(cur.fetchone())
        cur.execute("""
            SELECT column_name, data_type, label, synonyms
            FROM nl2sql_columns WHERE table_id = %s
        """, (table_id,))
        cols = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT label FROM nl2sql_relations
            WHERE (from_table = %s OR to_table = %s)
              AND account_id = %s
        """, (table["table_name"], table["table_name"], table["account_id"]))
        rels = [dict(r) for r in cur.fetchall()]

    emb_text  = build_embedding_text(table, cols, rels)
    embedding = embedder.encode(emb_text).tolist()

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE nl2sql_tables
            SET embedding = %s::vector, indexed_at = NOW()
            WHERE id = %s
        """, (embedding, table_id))
    conn.commit()
    logger.info("🔄 Reindexé : table_id=%d", table_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=False, threaded=True)