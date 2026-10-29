"""
schema_service/search_data.py
-----------------------------
Lecture de l'index pour la recherche de schéma (pgvector). Séparé de server.py pour être
testable sans charger l'embedder.
"""

import psycopg2.extras

from . import search


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
        cur.execute("""
            SELECT table_name, column_name, value, normalized
            FROM nl2sql_values WHERE account_id = %s
        """, (account_id,))
        values = [dict(r) for r in cur.fetchall()]

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
    return candidates, tables_by_name, columns_by_table, relations, values


def vector_ranking(conn, account_id: int, q_vector: list[float], limit: int = 20) -> list[dict]:
    """
    Classement des tables par similarité vectorielle seule (sans mots, synonymes ni règles).
    Mesure directe de la qualité des fiches de tables et de l'embedder.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, 1 - (embedding <=> %s::vector) AS similarity
            FROM nl2sql_tables
            WHERE account_id = %s AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (q_vector, account_id, q_vector, limit))
        return [{"table_name": t, "similarity": round(float(s), 3)} for t, s in cur.fetchall()]
