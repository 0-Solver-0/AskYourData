"""
api_service/executor.py
-----------------------
Exécution en lecture seule de la requête validée, sur la base source (Odoo).

Connexion avec un rôle dédié (EXEC_DB_USER / EXEC_DB_PASSWORD, créé par
tools/create_readonly_role.py), jamais l'utilisateur odoo. Chaque requête :
  - transaction en lecture seule (SET TRANSACTION READ ONLY), annulée à la fin ;
  - statement_timeout (EXEC_TIMEOUT_MS, 10 s par défaut) ;
  - EXPLAIN d'abord : coût estimé au-delà de EXEC_MAX_COST → refus sans exécuter ;
  - au plus max_rows lignes lues (une de plus pour savoir si le résultat est tronqué).

    result = execute(sql)
    result["ok"], result["columns"], result["rows"], result["row_count"], result["truncated"]
    result["error"] → message exact de PostgreSQL (renvoyé au modèle SQL pour correction)
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import time
import uuid
from typing import Any, Optional

import psycopg2

from api_service.validate import MAX_ROWS


def exec_params(env: Optional[dict] = None) -> dict:
    env = env if env is not None else os.environ
    return {
        "host": env.get("SOURCE_DB_HOST", "postgres_odoo"),
        "port": int(env.get("SOURCE_DB_PORT") or 5432),
        "dbname": env.get("SOURCE_DB_NAME", "odoo"),
        "user": env.get("EXEC_DB_USER", "askyourdata"),
        "password": env.get("EXEC_DB_PASSWORD", ""),
        "timeout_ms": int(env.get("EXEC_TIMEOUT_MS") or 10000),
        "max_cost": float(env.get("EXEC_MAX_COST") or 5_000_000),
    }


def to_json_value(value: Any) -> Any:
    """Valeurs PostgreSQL → JSON, sans perte : le montant reste celui de la base."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value, default=str))
    return value


def _error_text(exc: Exception) -> str:
    """Message PostgreSQL utile (première ligne + indice), sans données de résultat."""
    pgerror = getattr(exc, "pgerror", None) or str(exc)
    lines = [ln for ln in pgerror.strip().splitlines() if ln.strip()]
    keep = [ln for ln in lines if ln.startswith(("ERROR", "HINT", "LINE", "DETAIL"))] or lines[:1]
    return " ".join(" ".join(keep).replace("EXPLAIN (FORMAT JSON) ", "").split())[:500]


def execute(sql: str, params: Optional[dict] = None, max_rows: int = MAX_ROWS, connect=psycopg2.connect) -> dict:
    p = params or exec_params()
    t0 = time.time()
    out: dict = {"ok": False, "columns": [], "rows": [], "row_count": 0, "truncated": False,
                 "error": None, "error_kind": None, "plan_cost": None, "elapsed_ms": 0}
    if not p.get("password"):
        out["error"], out["error_kind"] = "EXEC_DB_PASSWORD manquant dans .env (rôle en lecture seule)", "config"
        return out
    try:
        conn = connect(host=p["host"], port=p["port"], dbname=p["dbname"], user=p["user"],
                       password=p["password"], connect_timeout=5, application_name="askyourdata")
    except psycopg2.Error as exc:
        out["error"], out["error_kind"] = f"connexion à la base impossible : {_error_text(exc)}", "connection"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = %s", (p["timeout_ms"],))
            cur.execute("EXPLAIN (FORMAT JSON) " + sql)
            plan = cur.fetchone()[0]
            plan = json.loads(plan) if isinstance(plan, str) else plan
            cost = float(plan[0]["Plan"]["Total Cost"])
            out["plan_cost"] = cost
            if cost > p["max_cost"]:
                out["error"] = (f"query too expensive (estimated cost {cost:.0f} > {p['max_cost']:.0f}): "
                                "filter or aggregate more")
                out["error_kind"] = "cost"
                return out
            cur.execute(sql)
            out["columns"] = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            out["truncated"] = len(rows) > max_rows
            out["rows"] = [[to_json_value(v) for v in r] for r in rows[:max_rows]]
            out["row_count"] = len(out["rows"])
            out["ok"] = True
    except psycopg2.errors.QueryCanceled:
        out["error"] = f"query cancelled after {p['timeout_ms']} ms: filter or aggregate more"
        out["error_kind"] = "timeout"
    except psycopg2.Error as exc:
        out["error"], out["error_kind"] = _error_text(exc), "sql"
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def ping(params: Optional[dict] = None) -> dict:
    """Pour /health : le rôle se connecte-t-il, et est-il bien en lecture seule ?"""
    p = params or exec_params()
    if not p.get("password"):
        return {"status": "not_configured", "user": p["user"]}
    try:
        conn = psycopg2.connect(host=p["host"], port=p["port"], dbname=p["dbname"], user=p["user"],
                                password=p["password"], connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            read_only = cur.fetchone()[0] == "on"
        conn.close()
        return {"status": "ok" if read_only else "not_read_only", "user": p["user"], "db": p["dbname"]}
    except psycopg2.Error as exc:
        return {"status": "error", "user": p["user"], "error": _error_text(exc)}
