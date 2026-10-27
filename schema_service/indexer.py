"""
schema_service/indexer.py
-------------------------
Indexation d'une base source (Odoo) dans postgres_nl2sql :

  1. liste des tables de la base source
  2. métadonnées Odoo (libellés, aide, valeurs possibles, wizards)
  3. filtrage des tables techniques (config/index_filters.yaml), raisons enregistrées
  4. pour chaque table retenue : DDL, colonnes, clés, relations, nombre de lignes
  5. embeddings des colonnes et de la table (stockés)
  6. nettoyage des tables, colonnes et relations qui ne sont plus retenues

Règles :
  - L'indexation n'écrase JAMAIS label, description, synonyms, domain, notes,
    example, display : ils viennent des annotations manuelles ou de l'enrichissement.
  - Les colonnes sensibles (mots de passe, secrets, jetons) ne sont jamais indexées.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Callable, Optional

import psycopg2.extras
from psycopg2 import sql

from .filters import FilterConfig, TableInfo, decide_table, keep_column
from .odoo_meta import ModelMeta, load_odoo_metadata
from .text_builders import build_column_text, build_table_text

logger = logging.getLogger("schema_service.indexer")

EmbedFn = Callable[[list[str]], list[list[float]]]


# ── Lecture de la structure source (en une fois pour tout le schéma) ─────────

def _source_tables(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    return [r["table_name"] for r in cur.fetchall()]


def _source_columns(cur, schema: str) -> dict[str, list[dict]]:
    cur.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    out: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        out.setdefault(r["table_name"], []).append(dict(r))
    return out


def _source_keys(cur, schema: str) -> tuple[dict[str, set], dict[str, list[dict]]]:
    """Clés primaires et étrangères, via le catalogue (fiable même pour les clés composites)."""
    cur.execute(
        """
        SELECT con.contype,
               cl.relname   AS table_name,
               att.attname  AS column_name,
               rcl.relname  AS foreign_table,
               ratt.attname AS foreign_column
        FROM pg_constraint con
        JOIN pg_class cl      ON cl.oid = con.conrelid
        JOIN pg_namespace ns  ON ns.oid = cl.relnamespace
        LEFT JOIN pg_class rcl ON rcl.oid = con.confrelid
        JOIN LATERAL unnest(con.conkey, COALESCE(con.confkey, con.conkey)) AS k(attnum, fattnum) ON TRUE
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
        LEFT JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = k.fattnum
        WHERE ns.nspname = %s AND con.contype IN ('p', 'f')
        """,
        (schema,),
    )
    pks: dict[str, set] = {}
    fks: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        if r["contype"] == "p":
            pks.setdefault(r["table_name"], set()).add(r["column_name"])
        else:
            fks.setdefault(r["table_name"], []).append(dict(r))
    return pks, fks


def _row_count(cur, schema: str, table: str) -> Optional[int]:
    try:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)))
        return cur.fetchone()["n"]
    except Exception as exc:  # noqa: BLE001 — un comptage raté ne doit pas bloquer l'indexation
        logger.warning("Comptage impossible pour %s : %s", table, exc)
        cur.connection.rollback()
        return None


def _build_ddl(table: str, cols: list[dict], pk_cols: set) -> str:
    lines = [f"CREATE TABLE {table} ("]
    for col in cols:
        nullable = "" if col["is_nullable"] == "YES" else " NOT NULL"
        pk = " PRIMARY KEY" if col["column_name"] in pk_cols else ""
        lines.append(f"    {col['column_name']} {col['data_type']}{pk}{nullable},")
    lines.append(");")
    return "\n".join(lines)


# ── Filtrage ─────────────────────────────────────────────────────────────────

def plan_tables(table_names: list[str], meta: dict[str, ModelMeta], cfg: FilterConfig
                ) -> tuple[list[str], list[tuple[str, str]]]:
    """Retourne (tables retenues, [(table exclue, raison)])."""
    has_meta = bool(meta)
    kept, excluded = [], []
    for name in table_names:
        m = meta.get(name)
        info = TableInfo(name=name, odoo_model=m.model if m else None, transient=bool(m and m.transient))
        keep, reason = decide_table(info, cfg, has_meta)
        if keep:
            kept.append(name)
        else:
            excluded.append((name, reason))
    return kept, excluded


# ── Embeddings ───────────────────────────────────────────────────────────────

def reindex_table_embeddings(conn, table_id: int, embed: EmbedFn) -> None:
    """Recalcule et enregistre les embeddings des colonnes puis de la table (depuis la base NL2SQL)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM nl2sql_tables WHERE id = %s", (table_id,))
        table = dict(cur.fetchone())
        cur.execute(
            """
            SELECT id, column_name, data_type, label, synonyms, odoo_label, odoo_help, selection_values
            FROM nl2sql_columns WHERE table_id = %s ORDER BY id
            """,
            (table_id,),
        )
        cols = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT label FROM nl2sql_relations
            WHERE account_id = %s AND (from_table = %s OR to_table = %s) AND label IS NOT NULL
            """,
            (table["account_id"], table["table_name"], table["table_name"]),
        )
        rel_labels = [r["label"] for r in cur.fetchall()]

    texts = [build_column_text(c) for c in cols] + [build_table_text(table, cols, rel_labels)]
    vectors = embed(texts)
    col_vectors, table_vector = vectors[:-1], vectors[-1]

    with conn.cursor() as cur:
        if cols:
            psycopg2.extras.execute_batch(
                cur,
                "UPDATE nl2sql_columns SET embedding = %s::vector WHERE id = %s",
                [(list(v), c["id"]) for c, v in zip(cols, col_vectors)],
            )
        cur.execute(
            "UPDATE nl2sql_tables SET embedding = %s::vector, indexed_at = NOW() WHERE id = %s",
            (list(table_vector), table_id),
        )
    conn.commit()


# ── Indexation complète ──────────────────────────────────────────────────────

def index_account(conn, src, account: dict, embed: Optional[EmbedFn], cfg: FilterConfig,
                  dry_run: bool = False) -> dict:
    t0 = time.time()
    schema = account.get("pg_schema") or "public"
    account_id = account["id"]

    with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as scur:
        all_tables = _source_tables(scur, schema)
    meta = load_odoo_metadata(src, schema)
    kept, excluded = plan_tables(all_tables, meta, cfg)

    report = {
        "total_source": len(all_tables),
        "retained": len(kept),
        "excluded": len(excluded),
        "excluded_by_reason": dict(Counter(r for _, r in excluded).most_common()),
        "has_odoo_metadata": bool(meta),
        "dry_run": dry_run,
    }
    if dry_run:
        report["retained_tables"] = kept
        report["excluded_tables"] = [{"table_name": t, "reason": r} for t, r in excluded]
        report["elapsed_s"] = round(time.time() - t0, 2)
        return report

    with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as scur:
        columns_by_table = _source_columns(scur, schema)
        pks, fks = _source_keys(scur, schema)
    kept_set = set(kept)

    indexed, failed = 0, []
    for table_name in kept:
        try:
            m = meta.get(table_name)
            cols = [c for c in columns_by_table.get(table_name, []) if keep_column(c["column_name"], cfg)]
            pk_cols = pks.get(table_name, set())
            table_fks = [fk for fk in fks.get(table_name, [])
                         if fk["foreign_table"] in kept_set and keep_column(fk["column_name"], cfg)]
            fk_cols = {fk["column_name"] for fk in fks.get(table_name, [])}

            with src.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as scur:
                row_count = _row_count(scur, schema, table_name)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO nl2sql_tables
                        (account_id, schema_name, table_name, ddl, odoo_model, odoo_label, row_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (account_id, schema_name, table_name)
                    DO UPDATE SET ddl        = EXCLUDED.ddl,
                                  odoo_model = EXCLUDED.odoo_model,
                                  odoo_label = EXCLUDED.odoo_label,
                                  row_count  = EXCLUDED.row_count
                    RETURNING id
                    """,
                    (account_id, schema, table_name, _build_ddl(table_name, cols, pk_cols),
                     m.model if m else None, m.label if m else None, row_count),
                )
                table_id = cur.fetchone()[0]

                for col in cols:
                    f = m.fields.get(col["column_name"]) if m else None
                    cur.execute(
                        """
                        INSERT INTO nl2sql_columns
                            (table_id, column_name, data_type, is_nullable, is_pk, is_fk,
                             odoo_label, odoo_help, odoo_type, relation_model, selection_values)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (table_id, column_name)
                        DO UPDATE SET data_type        = EXCLUDED.data_type,
                                      is_nullable      = EXCLUDED.is_nullable,
                                      is_pk            = EXCLUDED.is_pk,
                                      is_fk            = EXCLUDED.is_fk,
                                      odoo_label       = EXCLUDED.odoo_label,
                                      odoo_help        = EXCLUDED.odoo_help,
                                      odoo_type        = EXCLUDED.odoo_type,
                                      relation_model   = EXCLUDED.relation_model,
                                      selection_values = EXCLUDED.selection_values
                        """,
                        (table_id, col["column_name"], col["data_type"], col["is_nullable"] == "YES",
                         col["column_name"] in pk_cols, col["column_name"] in fk_cols,
                         f.label if f else None, f.help if f else None, f.ttype if f else None,
                         f.relation if f else None, f.selection if f else None),
                    )

                # Colonnes disparues ou devenues sensibles
                cur.execute(
                    "DELETE FROM nl2sql_columns WHERE table_id = %s AND NOT (column_name = ANY(%s))",
                    (table_id, [c["column_name"] for c in cols]),
                )

                for fk in table_fks:
                    cur.execute(
                        """
                        INSERT INTO nl2sql_relations (account_id, from_table, from_column, to_table, to_column)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (account_id, table_name, fk["column_name"], fk["foreign_table"], fk["foreign_column"]),
                    )
            conn.commit()

            if embed is not None:
                reindex_table_embeddings(conn, table_id, embed)
            indexed += 1

        except Exception as exc:  # noqa: BLE001 — une table en échec ne doit pas arrêter l'indexation
            logger.warning("Table %s ignorée : %s", table_name, exc)
            conn.rollback()
            failed.append({"table_name": table_name, "error": str(exc)})

    # Nettoyage : tables qui ne sont plus retenues, relations orphelines, journal des exclusions
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM nl2sql_tables WHERE account_id = %s AND NOT (table_name = ANY(%s))",
            (account_id, kept),
        )
        report["removed_stale_tables"] = cur.rowcount
        cur.execute(
            """
            DELETE FROM nl2sql_relations
            WHERE account_id = %s AND (NOT (from_table = ANY(%s)) OR NOT (to_table = ANY(%s)))
            """,
            (account_id, kept, kept),
        )
        cur.execute("DELETE FROM nl2sql_index_exclusions WHERE account_id = %s", (account_id,))
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO nl2sql_index_exclusions (account_id, table_name, reason) VALUES (%s, %s, %s)",
            [(account_id, t, r) for t, r in excluded],
        )
    conn.commit()

    report.update({
        "indexed": indexed,
        "failed": failed,
        "embeddings": embed is not None,
        "elapsed_s": round(time.time() - t0, 1),
    })
    logger.info("Indexation : %d/%d tables retenues indexées, %d exclues, %d échecs, %ss",
                indexed, len(kept), len(excluded), len(failed), report["elapsed_s"])
    return report


def index_report(conn, account_id: int) -> dict:
    """État de l'index pour relecture : tables retenues (avec volume) et tables exclues (avec raison)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.table_name, t.odoo_model, t.odoo_label, t.row_count, t.enriched, t.manual_override,
                   (t.embedding IS NOT NULL) AS has_embedding,
                   (SELECT count(*) FROM nl2sql_columns c WHERE c.table_id = t.id) AS columns
            FROM nl2sql_tables t
            WHERE t.account_id = %s
            ORDER BY t.table_name
            """,
            (account_id,),
        )
        retained = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT table_name, reason FROM nl2sql_index_exclusions
            WHERE account_id = %s ORDER BY reason, table_name
            """,
            (account_id,),
        )
        excluded = [dict(r) for r in cur.fetchall()]
    return {
        "retained": len(retained),
        "retained_empty": sum(1 for t in retained if t["row_count"] == 0),
        "excluded": len(excluded),
        "excluded_by_reason": dict(Counter(e["reason"] for e in excluded).most_common()),
        "retained_tables": retained,
        "excluded_tables": excluded,
    }
