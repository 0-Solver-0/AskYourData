"""
schema_service/search_data.py
-----------------------------
Lecture de l'index pour la recherche de schéma (pgvector). Séparé de server.py pour être
testable sans charger l'embedder.
"""

from typing import Optional

import psycopg2.extras

from . import search, structured

# Score d'une table = son meilleur vecteur (libellé ou synonyme, table nl2sql_vectors) ; index d'avant
# la v8 (aucun vecteur multiple) : le vecteur unique de nl2sql_tables. Requête : %(q)s = vecteur.
TABLE_BEST_VECTOR = """
    LEFT JOIN LATERAL (
        SELECT v.text, 1 - (v.embedding <=> %(q)s::vector) AS sim FROM nl2sql_vectors v
        WHERE v.table_id = t.id AND v.column_id IS NULL AND v.embedding IS NOT NULL
        ORDER BY v.embedding <=> %(q)s::vector LIMIT 1
    ) m ON TRUE
"""
TABLE_SIMILARITY = ("COALESCE(m.sim, CASE WHEN t.embedding IS NULL THEN 0 "
                    "ELSE 1 - (t.embedding <=> %(q)s::vector) END)")


def load_search_data(conn, account_id: int, q_vector: list[float]):
    """
    Charge en une fois, pour un compte : tables (avec similarité à la question),
    colonnes (avec similarité) et relations. Quelques centaines de tables : quelques ms.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT t.id, t.table_name, t.odoo_model, t.label, t.odoo_label, t.synonyms, t.domain, t.description,
                   t.row_count, {TABLE_SIMILARITY} AS similarity, m.text AS matched_text
            FROM nl2sql_tables t {TABLE_BEST_VECTOR}
            WHERE t.account_id = %(account)s
        """, {"q": q_vector, "account": account_id})
        tables = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT t.table_name, c.column_name, c.data_type, c.is_pk, c.is_fk, c.is_nullable,
                   c.label, c.synonyms, c.notes, c.display, c.always_include, c.odoo_label, c.odoo_type, c.selection_values,
                   COALESCE(m.sim, CASE WHEN c.embedding IS NULL THEN 0 ELSE 1 - (c.embedding <=> %(q)s::vector) END)
                       AS similarity
            FROM nl2sql_columns c JOIN nl2sql_tables t ON t.id = c.table_id
            LEFT JOIN LATERAL (
                SELECT 1 - (v.embedding <=> %(q)s::vector) AS sim FROM nl2sql_vectors v
                WHERE v.column_id = c.id AND v.embedding IS NOT NULL
                ORDER BY v.embedding <=> %(q)s::vector LIMIT 1
            ) m ON TRUE
            WHERE t.account_id = %(account)s
            ORDER BY t.table_name, c.id
        """, {"q": q_vector, "account": account_id})
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
    Mesure directe de la qualité des fiches de tables et de l'embedder. « matched » : le texte
    (libellé ou synonyme) qui a donné le score.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT * FROM (
                SELECT t.table_name, {TABLE_SIMILARITY} AS similarity, m.text
                FROM nl2sql_tables t {TABLE_BEST_VECTOR}
                WHERE t.account_id = %(account)s AND (t.embedding IS NOT NULL OR m.sim IS NOT NULL)
            ) s ORDER BY similarity DESC LIMIT %(limit)s
        """, {"q": q_vector, "account": account_id, "limit": limit})
        return [{"table_name": t, "similarity": round(float(s), 3), "matched": m} for t, s, m in cur.fetchall()]


def column_similarities(conn, account_id: int, table_names: list[str], vectors: list[list[float]],
                        matched: Optional[dict[tuple[str, str], list[Optional[str]]]] = None,
                        ) -> dict[tuple[str, str], list[float]]:
    """
    Similarité de chaque vecteur (un par morceau de question) avec chaque colonne des tables
    données, en une requête : {(table, colonne): [similarité morceau 0, 1…]}.
    Score d'une colonne = son meilleur vecteur (libellé ou synonyme) ; sans vecteurs multiples,
    son vecteur unique. `matched`, s'il est fourni, reçoit le texte gagnant de chaque score.
    """
    if not table_names or not vectors:
        return {}
    values_sql = ", ".join(["(%s, %s::vector)"] * len(vectors))
    params: list = []
    for i, vec in enumerate(vectors):
        params += [i, vec]
    params += [account_id, table_names]
    params = params + params
    out: dict[tuple[str, str], list[float]] = {}
    with conn.cursor() as cur:
        cur.execute(f"""
            WITH s AS (
                SELECT c.id, t.table_name, c.column_name, e.i, 1 - (v.embedding <=> e.v) AS sim, v.text
                FROM nl2sql_columns c
                JOIN nl2sql_tables t ON t.id = c.table_id
                JOIN nl2sql_vectors v ON v.column_id = c.id AND v.embedding IS NOT NULL
                CROSS JOIN (VALUES {values_sql}) AS e(i, v)
                WHERE t.account_id = %s AND t.table_name = ANY(%s)
                UNION ALL
                SELECT c.id, t.table_name, c.column_name, e.i, 1 - (c.embedding <=> e.v), NULL
                FROM nl2sql_columns c
                JOIN nl2sql_tables t ON t.id = c.table_id
                CROSS JOIN (VALUES {values_sql}) AS e(i, v)
                WHERE t.account_id = %s AND t.table_name = ANY(%s) AND c.embedding IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM nl2sql_vectors x WHERE x.column_id = c.id)
            )
            SELECT DISTINCT ON (id, i) table_name, column_name, i, sim, text FROM s ORDER BY id, i, sim DESC
        """, params)
        for table, column, i, sim, text in cur.fetchall():
            sims = out.setdefault((table, column), [0.0] * len(vectors))
            sims[i] = float(sim)
            if matched is not None:
                matched.setdefault((table, column), [None] * len(vectors))[i] = text
    return out


def run_structured(conn, embedder, account_id: int, question: str, sq, params,
                   missing: Optional[list[str]] = None, subject: Optional[str] = None) -> tuple[dict, list]:
    """
    Recherche structurée ; avec `missing`, recherche complémentaire des mots manquants (colonnes des
    tables retenues et de leurs voisines, puis fiches de tables) et plan complété.
    `subject` : table du sujet imposée (choix du juge parmi les candidates).
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

    # Entités des morceaux qui ne portent pas sur le sujet (« vendor », « customer »…) : table par les fiches
    entity_names = list(dict.fromkeys(e for e in (sq.entity_of(text) for _, text in pieces) if e))
    entity_vecs = embedder.embed_queries(entity_names, "tables") if entity_names else []

    candidates, tables_by_name, columns_by_table, relations, values = \
        load_search_data(conn, account_id, subject_vec)
    if not candidates:
        raise LookupError("Aucune table indexée — lancez /index d'abord")
    table_sims = {c.table_name: c.vector_score for c in candidates}
    ranking = structured.order_subjects(table_sims, tables_by_name, relations, subject)
    names = ranking[:params.candidate_tables]
    lines = structured.line_table(ranking[0], relations, tables_by_name) if ranking else None
    if lines and lines not in names:
        names.append(lines)
    # Quelques tables possibles par entité (« vendor » : product_supplierinfo, res_partner…) : le plan
    # choisit celle que le sujet référence par une clé, sinon la première
    entity_tables: dict[str, dict] = {}
    for name, vec in zip(entity_names, entity_vecs):
        top = [r for r in vector_ranking(conn, account_id, vec, limit=structured.ENTITY_CANDIDATES)
               if r["similarity"] >= params.table_piece_min]
        if top:
            entity_tables[name] = {"entity": name, "candidates": [
                {"table": r["table_name"], "similarity": r["similarity"], "via": r["matched"]} for r in top]}
    piece_entities = [entity_tables.get(sq.entity_of(text) or "") for _, text in pieces]
    # Tables des entités et leurs voisines directes (un attribut peut être à un lien : product_template)
    forward = structured.build_forward_graph(relations)
    for e in entity_tables.values():
        for c in e["candidates"]:
            for t in [c["table"], *sorted(forward.get(c["table"], ()))]:
                if t not in names and t in tables_by_name:
                    names.append(t)
    column_texts: dict = {}
    column_sims = column_similarities(conn, account_id, names, piece_vecs, column_texts)
    piece_table_sims: list[dict[str, float]] = [{} for _ in pieces]
    for i, vec in zip(group_idx, group_vecs):
        piece_table_sims[i] = {r["table_name"]: r["similarity"]
                               for r in vector_ranking(conn, account_id, vec, limit=5)}

    value_text = question or " ".join([sq.subject, *sq.filters])
    value_matches = search.match_values(value_text, values)
    plan = structured.plan_structured(sq, table_sims, column_sims, tables_by_name, columns_by_table,
                                      relations, params, value_matches, piece_table_sims, subject=subject,
                                      piece_entities=piece_entities, column_texts=column_texts)
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
                                              extra_columns=extra_cols, extra_tables=extra_tabs,
                                              subject=subject, piece_entities=piece_entities,
                                              column_texts=column_texts)
    return plan, report
