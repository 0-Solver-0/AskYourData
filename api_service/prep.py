"""
api_service/prep.py
-------------------
Rôle PREP : découpage d'une question en morceaux de recherche (voir prompts/prep_structure.md).

    result = structure_question(llm_client, "Top 5 customers by revenue")
    result["status"]      → ok | write_request | out_of_scope | ambiguous | degraded
    result["structured"]  → {"subject", "measures", "filters", "group_by", "period", "order"}
                            (format attendu par POST /select/structured de schema_service)

Le modèle ne voit que la question, jamais le schéma : il ne peut pas inventer de table.
Si l'appel échoue (fournisseur et secours indisponibles, réponse illisible), le statut est
« degraded » et la question entière sert de sujet : la recherche continue sans découpage.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Optional

from api_service.llm import LLMClient, LLMError, extract_json

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "prep_structure.md")
STATUSES = ("ok", "write_request", "out_of_scope", "ambiguous")
LIST_KEYS = ("measures", "filters", "group_by")
MAX_PIECE_WORDS = 6        # au-delà, ce n'est plus un morceau mais une phrase : on le garde, signalé
MAX_TOKENS = 2000          # une question, raisonnement « low » compris


@lru_cache(maxsize=4)
def load_prompt(path: str = PROMPT_PATH) -> str:
    """Premier bloc ```text … ``` du fichier : le prompt système, tel quel."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    match = re.search(r"```text\n(.*?)\n```", content, re.S)
    if not match:
        raise ValueError(f"{path} : bloc ```text``` introuvable")
    return match.group(1).strip()


def _clean(value: Any) -> Optional[str]:
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def normalize(raw: Any) -> dict:
    """
    Réponse du modèle → {"status", "reason", "structured", "warnings"}.
    Tolère les écarts courants (chaîne au lieu de liste, tableau d'un seul objet, clés manquantes).
    """
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ValueError("la réponse n'est pas un objet JSON")
    warnings: list[str] = []
    status = (_clean(raw.get("status")) or "ok").lower()
    if status not in STATUSES:
        warnings.append(f"statut inconnu « {status} », traité comme ok")
        status = "ok"
    reason = _clean(raw.get("reason"))

    structured: dict[str, Any] = {"subject": _clean(raw.get("subject"))}
    for key in LIST_KEYS:
        value = raw.get(key) or []
        if isinstance(value, str):
            value = [value]
        items = [t for t in (_clean(v) for v in value if not isinstance(v, (dict, list))) if t]
        long = [t for t in items if len(t.split()) > MAX_PIECE_WORDS]
        if long:
            warnings.append(f"{key} : morceau long {long}")
        structured[key] = items
    structured["period"] = _clean(raw.get("period"))
    structured["order"] = _clean(raw.get("order"))

    if status == "ok" and not structured["subject"]:
        raise ValueError("statut ok sans sujet")
    return {"status": status, "reason": reason, "structured": structured, "warnings": warnings}


def degraded(question: str, error: str) -> dict:
    """Repli sans découpage : la question entière sert de sujet."""
    return {"status": "degraded", "reason": error, "warnings": [],
            "structured": {"subject": question, "measures": [], "filters": [], "group_by": [],
                           "period": None, "order": None}}


def structure_question(client: LLMClient, question: str, *, reasoning: Optional[str] = None) -> dict:
    """Découpe une question. Renvoie aussi la télémétrie de l'appel (« llm »)."""
    messages = [{"role": "system", "content": load_prompt()},
                {"role": "user", "content": f"Question: {question.strip()}"}]
    try:
        result = client.complete("prep", messages, max_tokens=MAX_TOKENS, reasoning=reasoning)
    except LLMError as exc:
        out = degraded(question, f"PREP indisponible : {exc}")
        out["llm"] = {"attempts": exc.attempts}
        return out
    try:
        out = normalize(extract_json(result.text))
    except ValueError as exc:
        out = degraded(question, f"réponse PREP illisible : {exc}")
        out["raw"] = result.text[:500]
    out["llm"] = result.telemetry()
    return out
