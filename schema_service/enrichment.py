"""
schema_service/enrichment.py
----------------------------
Export et import de l'enrichissement (descriptions, libellés, synonymes), par module Odoo.

Export : un fichier JSON par module (ou par liste de tables) avec, pour chaque table retenue,
tout ce que l'index sait d'elle (en lecture seule) et les champs à remplir dans « enrich ».
Import : le même fichier, rempli à la main ou par le rôle PREP. Seuls les champs « enrich »
sont appliqués ; les tables modifiées sont ensuite réencodées (voir server.py).

Règles de l'import :
  - champ absent ou null : inchangé ; chaîne vide : effacé ; liste : jointe par des virgules
  - table ou colonne inconnue : signalée, jamais créée
  - les champs importés sont protégés : une réindexation ne les écrase pas
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extras

from .text_builders import TECHNICAL_COLUMNS

FORMAT = "askyourdata-enrichment"
VERSION = 1

TABLE_FIELDS = ("label", "description", "synonyms", "domain")
COLUMN_FIELDS = ("label", "synonyms", "notes")

INSTRUCTIONS = (
    "Remplir uniquement les blocs « enrich » (en anglais, comme les questions). "
    "Table : description = ce que contient la table et à quoi elle sert (1 à 3 phrases, "
    "sans viser une question précise) ; synonyms = termes métier, séparés par des virgules ; "
    "label = nom métier court ; domain = sales, accounting, inventory, crm, hr… "
    "Colonne : label, synonyms, notes (sens métier, unité, piège). "
    "null = inchangé, \"\" = effacer. Le reste du fichier est ignoré à l'import."
)


def list_modules(conn, account_id: int) -> list[dict]:
    """Modules Odoo présents dans l'index, avec le nombre de tables et de tables enrichies."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COALESCE(odoo_module, '(inconnu)') AS module, count(*) AS tables,
                   count(*) FILTER (WHERE enriched) AS enriched,
                   COALESCE(sum(row_count), 0)::bigint AS rows
            FROM nl2sql_tables WHERE account_id = %s
            GROUP BY 1 ORDER BY 2 DESC, 1
        """, (account_id,))
        return [dict(r) for r in cur.fetchall()]


def export_schema(conn, account_id: int, module: Optional[str] = None,
                  tables: Optional[list[str]] = None, include_technical: bool = False) -> dict:
    """Fichier d'enrichissement pour un module ou une liste de tables (les deux : intersection)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where, params = ["account_id = %s"], [account_id]
        if module:
            where.append("odoo_module = %s")
            params.append(module)
        if tables:
            where.append("table_name = ANY(%s)")
            params.append(list(tables))
        cur.execute(f"""
            SELECT id, table_name, odoo_model, odoo_module, odoo_label, row_count,
                   label, description, synonyms, domain, enriched, enriched_by
            FROM nl2sql_tables WHERE {' AND '.join(where)}
            ORDER BY row_count DESC NULLS LAST, table_name
        """, params)
        table_rows = [dict(r) for r in cur.fetchall()]
        ids = [t["id"] for t in table_rows]
        names = [t["table_name"] for t in table_rows]

        cur.execute("""
            SELECT table_id, column_name, data_type, is_pk, is_fk, odoo_label, odoo_help, odoo_type,
                   selection_values, label, synonyms, notes
            FROM nl2sql_columns WHERE table_id = ANY(%s) ORDER BY table_id, id
        """, (ids,))
        cols_by_table: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            cols_by_table.setdefault(r["table_id"], []).append(dict(r))

        cur.execute("""
            SELECT from_table, from_column, to_table, to_column FROM nl2sql_relations
            WHERE account_id = %s AND (from_table = ANY(%s) OR to_table = ANY(%s))
        """, (account_id, names, names))
        relations = [dict(r) for r in cur.fetchall()]

    out_refs = {(r["from_table"], r["from_column"]): f"{r['to_table']}.{r['to_column']}" for r in relations}
    referenced_by: dict[str, list[str]] = {}
    for r in relations:
        referenced_by.setdefault(r["to_table"], []).append(f"{r['from_table']}.{r['from_column']}")

    exported = []
    for t in table_rows:
        columns = []
        for c in cols_by_table.get(t["id"], []):
            if not include_technical and (c["column_name"] in TECHNICAL_COLUMNS or c["is_pk"]):
                continue
            columns.append({
                "column_name": c["column_name"],
                "data_type": c["data_type"],
                "odoo_type": c["odoo_type"],
                "odoo_label": c["odoo_label"],
                "odoo_help": c["odoo_help"],
                "values": c["selection_values"],
                "references": out_refs.get((t["table_name"], c["column_name"])),
                "enrich": {k: c[k] for k in COLUMN_FIELDS},
            })
        exported.append({
            "table_name": t["table_name"],
            "odoo_model": t["odoo_model"],
            "odoo_module": t["odoo_module"],
            "odoo_label": t["odoo_label"],
            "row_count": t["row_count"],
            "enriched_by": t["enriched_by"] if t["enriched"] else None,
            "referenced_by": sorted(referenced_by.get(t["table_name"], [])),
            "enrich": {k: t[k] for k in TABLE_FIELDS},
            "columns": columns,
        })

    return {
        "format": FORMAT,
        "version": VERSION,
        "account_id": account_id,
        "module": module,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instructions": INSTRUCTIONS,
        "tables": exported,
    }


def _normalize(value: Any) -> tuple[bool, Optional[str]]:
    """(à appliquer ?, valeur) : None → inchangé ; "" → effacer (NULL) ; liste → « a, b »."""
    if value is None:
        return False, None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    value = " ".join(str(value).split())
    return True, (value or None)


def import_enrichment(conn, account_id: int, payload: dict, dry_run: bool = False,
                      source: str = "manual") -> dict:
    """
    Applique les blocs « enrich » du fichier. Retourne le rapport et, si rien n'est simulé,
    les identifiants des tables à réencoder (« changed_table_ids »).
    """
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError(f"Fichier non reconnu : « format » doit valoir « {FORMAT} »")
    entries = payload.get("tables")
    if not isinstance(entries, list):
        raise ValueError("Fichier non reconnu : « tables » doit être une liste")

    changes, unknown_tables, unknown_columns, ignored = [], [], [], set()
    changed_tables: dict[int, str] = {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for entry in entries:
            name = (entry or {}).get("table_name")
            cur.execute(f"""
                SELECT id, {', '.join(TABLE_FIELDS)} FROM nl2sql_tables
                WHERE account_id = %s AND table_name = %s
            """, (account_id, name))
            row = cur.fetchone()
            if not row:
                unknown_tables.append(name)
                continue
            table_id = row["id"]

            enrich = entry.get("enrich") or {}
            ignored |= {f"table.{k}" for k in enrich if k not in TABLE_FIELDS}
            updates = {}
            for field in TABLE_FIELDS:
                apply, value = _normalize(enrich.get(field))
                if apply and value != row[field]:
                    updates[field] = value
                    changes.append({"table": name, "column": None, "field": field,
                                    "old": row[field], "new": value})
            if updates and not dry_run:
                sets = ", ".join(f"{k} = %s" for k in updates)
                cur.execute(f"""
                    UPDATE nl2sql_tables SET {sets}, enriched = TRUE, enriched_by = %s, enriched_at = NOW(),
                           manual_override = manual_override OR %s
                    WHERE id = %s
                """, [*updates.values(), source, source == "manual", table_id])
            if updates:
                changed_tables[table_id] = name

            cur.execute(f"""
                SELECT id, column_name, {', '.join(COLUMN_FIELDS)} FROM nl2sql_columns WHERE table_id = %s
            """, (table_id,))
            current = {r["column_name"]: dict(r) for r in cur.fetchall()}
            for col in entry.get("columns") or []:
                cname = (col or {}).get("column_name")
                cenrich = (col or {}).get("enrich") or {}
                if cname not in current:
                    if any(v is not None for v in cenrich.values()):
                        unknown_columns.append(f"{name}.{cname}")
                    continue
                ignored |= {f"column.{k}" for k in cenrich if k not in COLUMN_FIELDS}
                cupdates = {}
                for field in COLUMN_FIELDS:
                    apply, value = _normalize(cenrich.get(field))
                    if apply and value != current[cname][field]:
                        cupdates[field] = value
                        changes.append({"table": name, "column": cname, "field": field,
                                        "old": current[cname][field], "new": value})
                if cupdates and not dry_run:
                    sets = ", ".join(f"{k} = %s" for k in cupdates)
                    cur.execute(f"UPDATE nl2sql_columns SET {sets}, manual_override = TRUE WHERE id = %s",
                                [*cupdates.values(), current[cname]["id"]])
                if cupdates:
                    changed_tables[table_id] = name

    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    return {
        "dry_run": dry_run,
        "tables_in_file": len(entries),
        "tables_changed": sorted(changed_tables.values()),
        "fields_changed": len(changes),
        "changes": changes,
        "unknown_tables": unknown_tables,
        "unknown_columns": unknown_columns,
        "ignored_fields": sorted(ignored),
        "changed_table_ids": [] if dry_run else list(changed_tables),
    }
