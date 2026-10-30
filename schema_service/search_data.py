"""
schema_service/search_data.py
-----------------------------
Lecture de l'index pour la recherche de schéma (pgvector). Séparé de server.py pour être
testable sans charger l'embedder.
"""

from typing import Optional

import psycopg2.extras

from . import search, structured


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
                   c.label, c.synonyms, c.notes, c.display, c.always_include, c.odoo_label, c.odoo_type, c.selection_values,
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


def column_similarities(conn, account_id: int, table_names: list[str],
                        vectors: list[list[float]]) -> dict[tuple[str, str], list[float]]:
    """
    Similarité de chaque vecteur (un par morceau de question) avec chaque colonne des tables
    données, en une requête : {(table, colonne): [similarité morceau 0, 1…]}.
    """
    if not table_names or not vectors:
        return {}
    values_sql = ", ".join(["(%s, %s::vector)"] * len(vectors))
    params: list = []
    for i, vec in enumerate(vectors):
        params += [i, vec]
    params += [account_id, table_names]
    out: dict[tuple[str, str], list[float]] = {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT t.table_name, c.column_name, e.i, 1 - (c.embedding <=> e.v)
            FROM nl2sql_columns c
            JOIN nl2sql_tables t ON t.id = c.table_id
            CROSS JOIN (VALUES {values_sql}) AS e(i, v)
            WHERE t.account_id = %s AND t.table_name = ANY(%s) AND c.embedding IS NOT NULL
        """, params)
        for table, column, i, sim in cur.fetchall():
            sims = out.setdefault((table, column), [0.0] * len(vectors))
            sims[i] = float(sim)
    return out


def run_structured(conn, embedder, account_id: int, question: str, sq, params,
                   missing: Optional[list[str]] = None) -> tuple[dict, list]:
    """
    Recherche structurée ; avec `missing`, recherche complémentaire des mots manquants (colonnes des
    tables retenues et de leurs voisines, puis fiches de tables) et plan complété.
    Renvoie (plan, rapport des mots manquants).
    """
    pieces = sq.pieces()
    subject_vec = embedder.embed_query(sq.subject, "tables")
    piece_vecs  = embedder.embed_queries([text for _, text in pieces], "columns")
    # Regroupements comparés aussi aux fiches de tables (« product category » → product_category)
    group_idx = [i for i, (role, _) in enumerate(pieces) if role == "group_by"]
    group_vecs = embedder.embed_queries([pieces[i][1] for i in group_idx], "tables") if group_idx else []
    missing = [m for m in (missing or []) if m][:5]
    missing_col_vecs = embedder.embed_queries(missing, "columns") if missing else []
    missing_tab_vecs = embedder.embed_queries(missing, "tables") if missing else []

    candidates, tables_by_name, columns_by_table, relations, values = \
        load_search_data(conn, account_id, subject_vec)
    if not candidates:
        raise LookupError("Aucune table indexée — lancez /index d'abord")
    table_sims = {c.table_name: c.vector_score for c in candidates}
    names = sorted(table_sims, key=table_sims.get, reverse=True)[:params.candidate_tables]
    column_sims = column_similarities(conn, account_id, names, piece_vecs)
    piece_table_sims: list[dict[str, float]] = [{} for _ in pieces]
    for i, vec in zip(group_idx, group_vecs):
        piece_table_sims[i] = {r["table_name"]: r["similarity"]
                               for r in vector_ranking(conn, account_id, vec, limit=5)}

    value_text = question or " ".join([sq.subject, *sq.filters])
    value_matches = search.match_values(value_text, values)
    plan = structured.plan_structured(sq, table_sims, column_sims, tables_by_name, columns_by_table,
                                      relations, params, value_matches, piece_table_sims)
    report: list = []
    if missing:
        selected = [t["table_name"] for t in plan["tables"]]
        around = structured.neighbor_tables(selected, relations, tables_by_name)
        missing_sims = column_similarities(conn, account_id, around, missing_col_vecs)
        missing_table_sims = [{r["table_name"]: r["similarity"]
                               for r in vector_ranking(conn, account_id, vec, limit=5)}
                              for vec in missing_tab_vecs]
        present = {(t["table_name"], c["column_name"]) for t in plan["tables"] for c in t["columns"]}
        extra_cols, extra_tabs, report = structured.find_missing(
            missing, selected, missing_sims, missing_table_sims, tables_by_name, columns_by_table,
            relations, params, present=present)
        if extra_cols or extra_tabs:
            plan = structured.plan_structured(sq, table_sims, column_sims, tables_by_name, columns_by_table,
                                              relations, params, value_matches, piece_table_sims,
                                              extra_columns=extra_cols, extra_tables=extra_tabs)
    return plan, report
