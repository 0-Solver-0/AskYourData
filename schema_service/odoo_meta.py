"""
schema_service/odoo_meta.py
---------------------------
Lecture des métadonnées qu'Odoo stocke déjà dans sa base (gratuit, sans LLM) :

  ir_model                    → modèle, libellé de la table, modèle transitoire (wizard)
  ir_model_fields             → libellé, aide, type et relation de chaque champ stocké
  ir_model_fields_selection   → valeurs possibles des champs « selection »

Depuis Odoo 16, les champs traduisibles (name, field_description, help) sont stockés
en JSONB ({"en_US": "..."}). Les versions plus anciennes les stockent en texte.
Le type réel est détecté pour produire la bonne expression SQL.

Si la base source n'est pas une base Odoo (pas de table ir_model), tout est vide
et l'indexation continue avec la seule structure PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import psycopg2.extras
from psycopg2.extensions import quote_ident

from .filters import table_name_for_model
from .text_builders import format_selection

logger = logging.getLogger("schema_service.odoo_meta")


@dataclass
class FieldMeta:
    label: Optional[str] = None
    help: Optional[str] = None
    ttype: Optional[str] = None
    relation: Optional[str] = None
    selection: Optional[str] = None


@dataclass
class ModelMeta:
    model: str
    label: Optional[str] = None
    transient: bool = False
    fields: dict[str, FieldMeta] = field(default_factory=dict)


def _column_types(cur, schema: str, table: str) -> dict[str, str]:
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {r["column_name"]: r["data_type"] for r in cur.fetchall()}


def _text_expr(alias: str, column: str, types: dict[str, str]) -> str:
    """Expression SQL renvoyant le texte anglais d'un champ, JSONB traduisible ou texte simple."""
    if column not in types:
        return "NULL"
    ref = f"{alias}.{column}"
    if types[column] in ("jsonb", "json"):
        return f"COALESCE({ref}->>'en_US', (SELECT v FROM jsonb_each_text({ref}::jsonb) AS t(k, v) LIMIT 1))"
    return f"{ref}::text"


def load_odoo_metadata(src_conn, schema: str = "public") -> dict[str, ModelMeta]:
    """
    Retourne {nom_de_table: ModelMeta}. Dictionnaire vide si la base n'est pas une base Odoo.
    """
    with src_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        model_types = _column_types(cur, schema, "ir_model")
        if not model_types:
            logger.info("Pas de table ir_model : base non Odoo, métadonnées ignorées")
            return {}

        sch = quote_ident(schema, cur)
        transient_expr = "m.transient" if "transient" in model_types else "FALSE"
        cur.execute(
            f"""
            SELECT m.id, m.model,
                   {_text_expr('m', 'name', model_types)} AS label,
                   {transient_expr} AS transient
            FROM {sch}.ir_model m
            """
        )
        models_by_name: dict[str, ModelMeta] = {}
        for r in cur.fetchall():
            models_by_name[r["model"]] = ModelMeta(
                model=r["model"], label=r["label"], transient=bool(r["transient"]),
            )

        field_types = _column_types(cur, schema, "ir_model_fields")
        fields_by_id: dict[int, tuple[str, str]] = {}
        if field_types:
            store_filter = "WHERE f.store" if "store" in field_types else ""
            cur.execute(
                f"""
                SELECT f.id, f.model, f.name, f.ttype, f.relation,
                       {_text_expr('f', 'field_description', field_types)} AS label,
                       {_text_expr('f', 'help', field_types)} AS help
                FROM {sch}.ir_model_fields f
                {store_filter}
                """
            )
            for r in cur.fetchall():
                meta = models_by_name.get(r["model"])
                if meta is None:
                    continue
                meta.fields[r["name"]] = FieldMeta(
                    label=r["label"], help=r["help"], ttype=r["ttype"], relation=r["relation"],
                )
                fields_by_id[r["id"]] = (r["model"], r["name"])

        sel_types = _column_types(cur, schema, "ir_model_fields_selection")
        if sel_types and fields_by_id:
            order = "s.field_id, s.sequence, s.id" if "sequence" in sel_types else "s.field_id, s.id"
            cur.execute(
                f"""
                SELECT s.field_id, s.value, {_text_expr('s', 'name', sel_types)} AS label
                FROM {sch}.ir_model_fields_selection s
                ORDER BY {order}
                """
            )
            values: dict[int, list[tuple[str, Optional[str]]]] = {}
            for r in cur.fetchall():
                values.setdefault(r["field_id"], []).append((r["value"], r["label"]))
            for field_id, pairs in values.items():
                model, fname = fields_by_id.get(field_id, (None, None))
                if model and fname in models_by_name[model].fields:
                    models_by_name[model].fields[fname].selection = format_selection(pairs)

    by_table = {table_name_for_model(m): meta for m, meta in models_by_name.items()}
    logger.info("Métadonnées Odoo : %d modèles", len(by_table))
    return by_table
