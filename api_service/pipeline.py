"""
api_service/pipeline.py
-----------------------
Chaîne d'une question, pilotée par le code (voir documentation/ARCHITECTURE.md § 5).

translate : découpage (PREP) → recherche structurée → génération SQL → si le modèle signale un
manque : recherche complémentaire des mots manquants puis UNE nouvelle génération.

ask : translate, puis vérification (api_service/validate.py) → exécution en lecture seule
(api_service/executor.py) → si erreur : le modèle SQL corrige avec l'erreur exacte (2 fois au plus)
→ réponse construite par le code à partir des lignes (api_service/answer.py).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from api_service.answer import build_answer
from api_service.llm import LLMClient
from api_service.prep import structure_question
from api_service.sqlgen import generate_sql
from api_service.validate import validate_sql

# schema_fn(account_id, question, structured) → réponse de POST /select/structured
SchemaFn = Callable[[int, str, dict], dict]
# extend_fn(account_id, question, structured, missing) → réponse de POST /select/extend
ExtendFn = Callable[[int, str, dict, list], dict]

# execute_fn(sql) → résultat de api_service.executor.execute
ExecuteFn = Callable[[str], dict]

MAX_CORRECTIONS = 2

REFUSALS = {"write_request": "refused", "out_of_scope": "refused", "ambiguous": "clarification_needed"}


def _usage(entry: Optional[dict]) -> dict:
    entry = entry or {}
    return {"provider": entry.get("provider"), "model": entry.get("model"),
            "tokens_in": entry.get("tokens_in", 0), "tokens_cached": entry.get("tokens_cached", 0),
            "tokens_out": entry.get("tokens_out", 0), "tokens_reasoning": entry.get("tokens_reasoning", 0),
            "cost_usd": entry.get("cost_usd", 0.0), "fallback_used": entry.get("fallback_used", False)}


def _add_usage(total: Optional[dict], part: dict) -> dict:
    if not total:
        return dict(part)
    out = dict(total)
    for key in ("tokens_in", "tokens_cached", "tokens_out", "tokens_reasoning"):
        out[key] = (out.get(key) or 0) + (part.get(key) or 0)
    out["cost_usd"] = round((out.get("cost_usd") or 0) + (part.get("cost_usd") or 0), 6)
    out["fallback_used"] = bool(out.get("fallback_used") or part.get("fallback_used"))
    return out


def translate(client: LLMClient, schema_fn: SchemaFn, question: str, account_id: int, *,
              structured: Optional[dict] = None, reasoning: Optional[dict] = None,
              extend_fn: Optional[ExtendFn] = None) -> dict:
    """
    Question → SQL (non exécuté). `structured` fourni : PREP est sauté (découpage écrit à la main).
    `reasoning` : {"prep": "low", "sql": "medium"} pour surcharger le raisonnement par rôle.
    """
    reasoning = reasoning or {}
    t0 = time.time()
    timings: dict[str, int] = {}
    out: dict = {"question": question, "status": None, "structured_request": None, "prep_status": None,
                 "assumption": None,
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
        out["assumption"] = prep.get("assumption")
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
    out["attempts"] = []
    gen = _generate(client, question, out, schema, reasoning, previous=None, kind="generation")
    if gen["status"] != "missing" or extend_fn is None:
        return _finish(out, t0)

    # 4. Le modèle signale un manque : recherche complémentaire, puis UNE nouvelle génération
    asked = gen["missing"]
    t = time.time()
    try:
        extended = extend_fn(account_id, question, out["structured_request"], asked)
    except Exception as exc:  # noqa: BLE001
        extended = {"error": str(exc)}
    timings["extend"] = int((time.time() - t) * 1000)
    report = extended.get("missing_report") or []
    out["missing_report"] = report
    found = [r for r in report if r.get("found")]
    if extended.get("error") or not found or not extended.get("schema_ddl"):
        out["status"] = "cannot_answer"
        out["reason"] = (f"Data not available: {', '.join(asked)}." if not extended.get("error")
                         else f"recherche complémentaire impossible : {extended['error']}")
        return _finish(out, t0)
    gen = _generate(client, question, out, extended, reasoning, previous={"missing": asked}, kind="missing")
    if gen["status"] == "missing":
        out["status"] = "cannot_answer"
        out["reason"] = f"Data not available: {', '.join(gen['missing'])}."
    return _finish(out, t0)


def ask(client: LLMClient, schema_fn: SchemaFn, execute_fn: ExecuteFn, question: str, account_id: int, *,
        structured: Optional[dict] = None, reasoning: Optional[dict] = None,
        extend_fn: Optional[ExtendFn] = None, max_corrections: int = MAX_CORRECTIONS) -> dict:
    """
    Question → réponse tirée de la base. Statut final : ok (answer rempli) ou celui de translate
    (cannot_answer, refused, clarification_needed…), ou error si aucune requête valide n'a été obtenue.
    """
    reasoning = reasoning or {}
    out = translate(client, schema_fn, question, account_id, structured=structured, reasoning=reasoning,
                    extend_fn=extend_fn)
    out["answer"], out["executed_sql"], out["checks"] = None, None, []
    if out["status"] != "sql":
        return out
    t0 = time.time() - out["timings_ms"]["total"] / 1000
    timings = out["timings_ms"]
    schema = {"schema_ddl": out["schema_ddl"], "tables": [{"table_name": t} for t in out["tables_used"]],
              "token_estimate": out["token_estimate"]}
    corrections = 0
    while True:
        t = time.time()
        check = validate_sql(out["sql"], out["schema_ddl"])
        timings["validation"] = timings.get("validation", 0) + int((time.time() - t) * 1000)
        step = {"sql": out["sql"], "valid": check["ok"], "error": check.get("error"),
                "warning": check.get("warning")}
        error = check.get("error")
        if check["ok"]:
            t = time.time()
            result = execute_fn(check["sql"])
            timings["execution"] = timings.get("execution", 0) + int((time.time() - t) * 1000)
            step.update({"executed": result["ok"], "error": result.get("error"),
                         "error_kind": result.get("error_kind"), "plan_cost": result.get("plan_cost"),
                         "row_count": result.get("row_count"), "elapsed_ms": result.get("elapsed_ms")})
            out["checks"].append(step)
            if result["ok"]:
                out["status"], out["executed_sql"] = "ok", check["sql"]
                out["answer"] = build_answer(result)
                return _finish(out, t0)
            if result.get("error_kind") in ("config", "connection"):
                out["status"], out["reason"] = "error", result["error"]
                return _finish(out, t0)
            error = result["error"]
        else:
            out["checks"].append(step)
        if corrections >= max_corrections:
            out["status"] = "error"
            out["reason"] = f"No valid query after {corrections} corrections: {error}"
            return _finish(out, t0)
        corrections += 1
        gen = _generate(client, question, out, schema, reasoning, previous={"sql": out["sql"], "error": error},
                        kind="correction")
        if gen["status"] != "sql":
            if gen["status"] == "missing":
                out["status"] = "cannot_answer"
                out["reason"] = f"Data not available: {', '.join(gen['missing'])}."
            return _finish(out, t0)


def _generate(client: LLMClient, question: str, out: dict, schema: dict, reasoning: dict,
              previous: Optional[dict], kind: str = "generation") -> dict:
    """Un essai de génération : met à jour `out` (statut, SQL, schéma de cet essai, usage, essais)."""
    t = time.time()
    gen = generate_sql(client, question, out["structured_request"], schema["schema_ddl"],
                       previous=previous, reasoning=reasoning.get("sql"), assumption=out.get("assumption"))
    elapsed = int((time.time() - t) * 1000)
    key = "generation" if not out["attempts"] else f"generation_{len(out['attempts']) + 1}"
    out["timings_ms"][key] = elapsed
    usage = _usage(gen.get("llm"))
    out["usage"]["sql"] = _add_usage(out["usage"].get("sql"), usage)
    out["models"]["sql"] = usage["model"] or out["models"].get("sql")
    tables = [t["table_name"] for t in schema.get("tables", [])]
    out["attempts"].append({"kind": kind, "tables": tables, "token_estimate": schema.get("token_estimate"),
                            "schema_ddl": schema["schema_ddl"], "status": gen["status"], "sql": gen["sql"],
                            "missing": gen["missing"], "reason": gen["reason"], "elapsed_ms": elapsed,
                            "cost_usd": usage["cost_usd"]})
    out["tables_used"], out["schema_ddl"] = tables, schema["schema_ddl"]
    out["token_estimate"] = schema.get("token_estimate")
    out["status"], out["sql"], out["missing"], out["reason"] = gen["status"], gen["sql"], gen["missing"], gen["reason"]
    if gen.get("raw"):
        out["raw"] = gen["raw"]
    return gen


def _finish(out: dict, t0: float) -> dict:
    out["timings_ms"]["total"] = int((time.time() - t0) * 1000)
    out["cost_usd"] = round(sum(u.get("cost_usd") or 0 for u in out["usage"].values()), 6)
    return out
