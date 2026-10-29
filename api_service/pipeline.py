"""
api_service/pipeline.py
-----------------------
Chaîne d'une question, pilotée par le code (voir documentation/ARCHITECTURE.md § 5).

Incrément 3 : découpage (PREP) → recherche structurée → génération SQL, sans exécution.
Les incréments suivants ajoutent la vérification et l'exécution (4), puis la relance sur
« missing » et sur erreur (5).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from api_service.llm import LLMClient
from api_service.prep import structure_question
from api_service.sqlgen import generate_sql

# schema_fn(account_id, question, structured) → réponse de POST /select/structured
SchemaFn = Callable[[int, str, dict], dict]

REFUSALS = {"write_request": "refused", "out_of_scope": "refused", "ambiguous": "clarification_needed"}


def _usage(entry: Optional[dict]) -> dict:
    entry = entry or {}
    return {"provider": entry.get("provider"), "model": entry.get("model"),
            "tokens_in": entry.get("tokens_in", 0), "tokens_cached": entry.get("tokens_cached", 0),
            "tokens_out": entry.get("tokens_out", 0), "tokens_reasoning": entry.get("tokens_reasoning", 0),
            "cost_usd": entry.get("cost_usd", 0.0), "fallback_used": entry.get("fallback_used", False)}


def translate(client: LLMClient, schema_fn: SchemaFn, question: str, account_id: int, *,
              structured: Optional[dict] = None, reasoning: Optional[dict] = None) -> dict:
    """
    Question → SQL (non exécuté). `structured` fourni : PREP est sauté (découpage écrit à la main).
    `reasoning` : {"prep": "low", "sql": "medium"} pour surcharger le raisonnement par rôle.
    """
    reasoning = reasoning or {}
    t0 = time.time()
    timings: dict[str, int] = {}
    out: dict = {"question": question, "status": None, "structured_request": None, "prep_status": None,
                 "sql": None, "missing": [], "reason": None, "tables_used": [], "schema_ddl": None,
                 "token_estimate": None, "models": {}, "usage": {}, "timings_ms": timings}

    # 1. Découpage
    if structured:
        out["structured_request"], out["prep_status"] = structured, "provided"
    else:
        t = time.time()
        prep = structure_question(client, question, reasoning=reasoning.get("prep"))
        timings["structuring"] = int((time.time() - t) * 1000)
        out["structured_request"], out["prep_status"] = prep["structured"], prep["status"]
        out["usage"]["prep"] = _usage(prep.get("llm"))
        out["models"]["prep"] = out["usage"]["prep"]["model"]
        if prep["status"] in REFUSALS:
            out["status"], out["reason"] = REFUSALS[prep["status"]], prep.get("reason")
            return _finish(out, t0)

    # 2. Recherche de schéma
    t = time.time()
    try:
        schema = schema_fn(account_id, question, out["structured_request"])
    except Exception as exc:  # noqa: BLE001
        out["status"], out["reason"] = "error", f"recherche de schéma impossible : {exc}"
        return _finish(out, t0)
    timings["schema"] = int((time.time() - t) * 1000)
    if schema.get("error") or not schema.get("schema_ddl"):
        out["status"], out["reason"] = "error", schema.get("error") or "schéma vide (index à construire ?)"
        return _finish(out, t0)
    out["tables_used"] = [t["table_name"] for t in schema.get("tables", [])]
    out["schema_ddl"], out["token_estimate"] = schema["schema_ddl"], schema.get("token_estimate")
    out["not_found"] = [p["text"] for p in (schema.get("debug") or {}).get("pieces", []) if p.get("not_found")]

    # 3. Génération SQL
    t = time.time()
    gen = generate_sql(client, question, out["structured_request"], schema["schema_ddl"],
                       reasoning=reasoning.get("sql"))
    timings["generation"] = int((time.time() - t) * 1000)
    out["usage"]["sql"] = _usage(gen.get("llm"))
    out["models"]["sql"] = out["usage"]["sql"]["model"]
    out["status"], out["sql"], out["missing"], out["reason"] = gen["status"], gen["sql"], gen["missing"], gen["reason"]
    if gen.get("raw"):
        out["raw"] = gen["raw"]
    return _finish(out, t0)


def _finish(out: dict, t0: float) -> dict:
    out["timings_ms"]["total"] = int((time.time() - t0) * 1000)
    out["cost_usd"] = round(sum(u.get("cost_usd") or 0 for u in out["usage"].values()), 6)
    return out
