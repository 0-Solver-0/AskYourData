"""
api_service/validate.py
-----------------------
Vérification du SQL écrit par le modèle, avant toute exécution (code, sans appel de modèle).

    check = validate_sql(sql, schema_ddl)
    check["ok"]     → True si la requête peut être exécutée
    check["error"]  → message précis, renvoyé tel quel au modèle SQL pour correction
    check["sql"]    → requête à exécuter (LIMIT ajouté si absent)

Règles :
- une seule instruction, SELECT ou WITH … SELECT ; aucune écriture, nulle part (CTE compris),
  ni SELECT INTO, ni FOR UPDATE, ni fonction à effet de bord ou d'accès au serveur ;
- uniquement les tables du schéma envoyé (jamais pg_catalog, information_schema…) ;
- uniquement les colonnes du schéma envoyé (résolution des alias et des CTE par sqlglot) ;
- jamais de colonne sensible (mots de passe, secrets, jetons) ni de SELECT * ;
- LIMIT ajouté à la fin si la requête n'en a pas.

Défense en profondeur : l'exécution se fait de toute façon avec un rôle PostgreSQL en lecture
seule, dans une transaction en lecture seule, avec un délai maximal (api_service/executor.py).
"""

from __future__ import annotations

import re
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from api_service.sensitive import FORBIDDEN_TABLES, SENSITIVE_COLUMN

MAX_ROWS = 1000

# Écritures et instructions hors lecture, où qu'elles apparaissent dans l'arbre
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
                   exp.Command, exp.Into, exp.Lock, exp.Copy, exp.TruncateTable, exp.Grant)

# Fonctions qui écrivent, attendent, lisent des fichiers ou exécutent une autre requête
FORBIDDEN_FUNCTIONS = {"nextval", "setval", "set_config", "current_setting", "query_to_xml",
                       "query_to_xml_and_xmlschema", "table_to_xml", "cursor_to_xml", "dblink",
                       "dblink_exec", "txid_current", "lo_import", "lo_export", "lo_unlink"}
FORBIDDEN_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink")


_TABLE_RE = re.compile(r"^CREATE TABLE\s+(\w+)\s*\(", re.M)
_COLUMN_RE = re.compile(r"^\s{2,}(\w+)\s+\S", re.M)


def schema_from_ddl(schema_ddl: str) -> dict[str, dict[str, str]]:
    """DDL compact (schema_service) → {table: {colonne: type}} pour sqlglot (types non utilisés)."""
    schema: dict[str, dict[str, str]] = {}
    blocks = _TABLE_RE.split(schema_ddl or "")
    for name, body in zip(blocks[1::2], blocks[2::2]):
        cols = {}
        for line in body.split("\n"):
            if line.strip().startswith((")", "FOREIGN KEY", "--")):
                if line.strip().startswith(")"):
                    break
                continue
            m = _COLUMN_RE.match(line)
            if m:
                cols[m.group(1)] = "TEXT"
        schema[name] = cols
    return schema


def _fail(error: str, sql: str, **extra) -> dict:
    return {"ok": False, "error": error, "sql": sql, **extra}


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower()
    try:
        return node.sql_name().lower()
    except Exception:  # noqa: BLE001
        return ""


def _has_star_projection(tree: exp.Expression) -> bool:
    """SELECT * ou t.* (COUNT(*) est permis)."""
    for select in tree.find_all(exp.Select):
        for e in select.expressions:
            if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
                return True
    return False


def _top_level_limit(tree: exp.Expression) -> Optional[exp.Expression]:
    return tree.args.get("limit") or tree.args.get("fetch")


def validate_sql(sql: str, schema_ddl: str, max_rows: int = MAX_ROWS) -> dict:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return _fail("empty query", sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as exc:
        # Requête que sqlglot ne sait pas lire : la base en jugera, avec le rôle en lecture seule
        return {"ok": True, "sql": _with_limit(sql, None, max_rows), "error": None,
                "parsed": False, "warning": f"non analysée par sqlglot : {str(exc).splitlines()[0]}"}
    if len(statements) != 1:
        return _fail(f"exactly one statement is allowed, got {len(statements)}", sql)
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return _fail(f"only SELECT queries are allowed, got {tree.key.upper()}", sql)
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return _fail(f"read-only: {node.key.upper()} is not allowed", sql)
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
                return _fail(f"function {name}() is not allowed", sql)
    if _has_star_projection(tree):
        return _fail("SELECT * is not allowed: list the columns needed", sql)

    schema = schema_from_ddl(schema_ddl)
    cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = set()
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or name in cte_names:
            continue
        if table.db and table.db.lower() not in ("public",):
            return _fail(f"table {table.db}.{name} is not allowed", sql)
        if name in FORBIDDEN_TABLES:
            return _fail(f"table {name} is not allowed (sensitive data)", sql)
        if name not in schema:
            return _fail(f"table {name} is not in the schema given; use only the tables listed", sql)
        tables.add(name)

    for column in tree.find_all(exp.Column):
        if SENSITIVE_COLUMN.search(column.name or ""):
            return _fail(f"column {column.name} is not allowed (sensitive data)", sql)
    try:
        qualify(tree.copy(), schema=schema, dialect="postgres", validate_qualify_columns=True,
                quote_identifiers=False, identify=False)
    except OptimizeError as exc:
        return _fail(f"{exc}; use only the columns listed in the schema", sql)
    except Exception as exc:  # noqa: BLE001 — limite de sqlglot : la base vérifiera les colonnes
        return {"ok": True, "sql": _with_limit(sql, tree, max_rows), "error": None, "parsed": True,
                "tables": sorted(tables), "warning": f"colonnes non vérifiées par sqlglot : {exc}"}

    return {"ok": True, "sql": _with_limit(sql, tree, max_rows), "error": None, "parsed": True,
            "tables": sorted(tables)}


def _with_limit(sql: str, tree: Optional[exp.Expression], max_rows: int) -> str:
    """Le texte du modèle est exécuté tel quel ; LIMIT est seulement ajouté à la fin s'il manque."""
    if tree is not None and _top_level_limit(tree) is not None:
        return sql
    if tree is None and re.search(r"\blimit\s+\d+\s*$", sql, re.I):
        return sql
    return f"{sql}\nLIMIT {max_rows}"
