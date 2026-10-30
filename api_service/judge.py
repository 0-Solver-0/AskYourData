"""
api_service/judge.py
--------------------
Juge du sujet : quand la recherche par vecteurs hésite entre les premières tables (scores serrés),
Muse Spark Open 30B (rôle prep) lit la question et les fiches des candidates et désigne la table
du sujet (prompts/subject_judge.md). Liste fermée : une réponse hors des candidates est ignorée,
et en cas d'échec le classement par vecteurs est gardé. Le juge aide, il ne bloque jamais.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from api_service.llm import LLMClient, LLMError, extract_json
from api_service.prep import load_prompt

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "subject_judge.md")
MAX_TOKENS = 1200
MAX_CANDIDATES = 8


def should_judge(ranking: list[dict], gap: float) -> bool:
    """Écart entre la 1re et la 2e table inférieur au seuil (0 : juge désactivé)."""
    if gap <= 0 or len(ranking) < 2:
        return False
    return float(ranking[0]["similarity"]) - float(ranking[1]["similarity"]) < gap


def build_user_message(question: str, structured: Optional[dict], candidates: list[dict]) -> str:
    lines = [f"Question: {question.strip()}",
             f"Subject words: {(structured or {}).get('subject') or ''}",
             "Candidates:"]
    for c in candidates:
        desc = " — ".join(x for x in (c.get("label"), c.get("description")) if x)
        lines.append(f"- {c['table_name']}: {desc}" if desc else f"- {c['table_name']}")
    return "\n".join(lines)


def pick_subject(client: LLMClient, question: str, structured: Optional[dict], ranking: list[dict], *,
                 reasoning: Optional[str] = None) -> dict:
    """Renvoie {"subject": table | None, "reason", "candidates", "llm"} ; jamais d'exception."""
    candidates = ranking[:MAX_CANDIDATES]
    names = [c["table_name"] for c in candidates]
    out: dict = {"subject": None, "reason": None, "candidates": names}
    messages = [{"role": "system", "content": load_prompt(PROMPT_PATH)},
                {"role": "user", "content": build_user_message(question, structured, candidates)}]
    try:
        result = client.complete("prep", messages, max_tokens=MAX_TOKENS, reasoning=reasoning)
    except LLMError as exc:
        out["reason"] = f"juge indisponible : {exc}"
        out["llm"] = {"attempts": exc.attempts}
        return out
    out["llm"] = result.telemetry()
    try:
        answer = extract_json(result.text)
    except ValueError:
        out["reason"] = "réponse du juge illisible"
        out["raw"] = result.text[:300]
        return out
    if not isinstance(answer, dict):
        out["reason"] = "réponse du juge illisible"
        return out
    chosen = answer.get("subject")
    out["reason"] = " ".join(str(answer.get("reason") or "").split()) or None
    if chosen in names:
        out["subject"] = chosen
    elif chosen:
        out["reason"] = f"hors des candidates ({json.dumps(chosen)}) : ignoré"
    return out
