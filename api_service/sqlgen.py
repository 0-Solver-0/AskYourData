"""
api_service/sqlgen.py
---------------------
Rôle SQL : écrit la requête à partir de la question, de son découpage et du schéma minimal
(voir prompts/sql_generate.md). Aucune exécution ici (incrément 4).

    out = generate_sql(llm_client, question, structured, schema_ddl)
    out["status"]  → sql | missing | cannot_answer | error
    out["sql"]     → la requête (status sql)
    out["missing"] → ce qui manque, en mots métier (status missing) : la recherche complémentaire
                     traduira ces mots en tables (incrément 5) ; le modèle ne nomme jamais de table
    out["reason"]  → phrase pour l'utilisateur (cannot_answer, error)
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from api_service.llm import LLMClient, LLMError, extract_json, extract_tag
from api_service.prep import load_prompt

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "sql_generate.md")
MAX_TOKENS = 6000            # requête + raisonnement


# « Employees by job position and manager » : des regroupements sans mesure se lisaient tantôt
# « compter par groupe », tantôt « lister les fiches triées ». Sauf si la question demande une liste,
# on fixe la lecture : une ligne par groupe, avec le nombre de fiches et, pour des documents qui portent
# un montant (paiements, factures, commandes), la somme du montant (« Payments … by journal » → total).
LIST_WORDS = re.compile(r"\b(list|lists|listing|names?|each|every|all)\b", re.I)


def grouping_reading(question: str, pieces: dict) -> Optional[str]:
    """Consigne ajoutée quand le découpage regroupe sans rien mesurer (question sans « liste »)."""
    group_by = pieces.get("group_by") or []
    if not group_by or pieces.get("measures") or LIST_WORDS.search(question or ""):
        return None
    subject = pieces.get("subject") or "record"
    return (f"Grouped by {', '.join(group_by)} with no measure: return one row per group, not the list of "
            f"records: the number of {subject} records (COUNT) and, when these records carry an amount "
            f"(payments, invoices, bills, orders), the sum of their main amount for the question.")


def build_user_message(question: str, structured: Optional[dict], schema_ddl: str,
                       previous: Optional[dict] = None, assumption: Optional[str] = None,
                       original: Optional[str] = None) -> str:
    """
    Question (reformulée par PREP s'il y a lieu) + question telle que posée (au cas où un détail se
    serait perdu) + découpage + schéma.
    """
    pieces = {k: v for k, v in (structured or {}).items() if v}
    parts = [f"Question: {question.strip()}"]
    if original and original.strip() != question.strip():
        parts.append(f"As the user typed or said it (may contain speech-to-text errors): {original.strip()}")
    if assumption:
        parts.append(f"Reading chosen for the user (follow it): {assumption.strip()}")
    grouping = grouping_reading(question, pieces)
    if grouping:
        parts.append(grouping)
    parts += ["Breakdown: " + json.dumps(pieces, ensure_ascii=False),
              "Schema:\n" + schema_ddl.strip()]
    if previous and previous.get("missing"):
        parts.append("You asked for: " + json.dumps(previous["missing"], ensure_ascii=False)
                     + ". The schema above now includes what the search found. If it is still not there, "
                     "use <cannot_answer>.")
    if previous and previous.get("error"):
        parts.append("Your previous query:\n" + (previous.get("sql") or "").strip()
                     + "\n\nwas rejected with this error:\n" + previous["error"].strip()
                     + "\n\nWrite a corrected query, with the same answer format. Use only the tables and "
                     "columns of the schema above.")
    return "\n\n".join(parts)


def parse_answer(text: str) -> dict:
    """Réponse du modèle → {"status", "sql", "missing", "reason"}. Tolère un bloc ```sql``` sans balise."""
    sql = extract_tag(text, "sql")
    if sql is None:
        fenced = re.search(r"```sql\s*(.*?)```", text or "", re.S | re.I)
        sql = fenced.group(1).strip() if fenced else None
    missing_raw = extract_tag(text, "missing")
    cannot = extract_tag(text, "cannot_answer")
    if sql:
        return {"status": "sql", "sql": sql.strip().rstrip(";").strip(), "missing": [], "reason": None}
    if missing_raw is not None:
        try:
            items = extract_json(missing_raw)
        except ValueError:
            items = [missing_raw]
        if isinstance(items, str):
            items = [items]
        items = [" ".join(str(i).split()) for i in items if str(i).strip()] if isinstance(items, list) else []
        if items:
            return {"status": "missing", "sql": None, "missing": items, "reason": None}
    if cannot:
        return {"status": "cannot_answer", "sql": None, "missing": [], "reason": " ".join(cannot.split())}
    return {"status": "error", "sql": None, "missing": [],
            "reason": "réponse du modèle SQL sans bloc <sql>, <missing> ni <cannot_answer>"}


def generate_sql(client: LLMClient, question: str, structured: Optional[dict], schema_ddl: str, *,
                 previous: Optional[dict] = None, reasoning: Optional[str] = None,
                 assumption: Optional[str] = None, original: Optional[str] = None) -> dict:
    messages = [{"role": "system", "content": load_prompt(PROMPT_PATH)},
                {"role": "user", "content": build_user_message(question, structured, schema_ddl, previous,
                                                               assumption, original)}]
    try:
        result = client.complete("sql", messages, max_tokens=MAX_TOKENS, reasoning=reasoning)
    except LLMError as exc:
        return {"status": "error", "sql": None, "missing": [], "reason": f"modèle SQL indisponible : {exc}",
                "llm": {"attempts": exc.attempts}}
    out = parse_answer(result.text)
    if out["status"] == "error":
        out["raw"] = result.text[:800]
    out["llm"] = result.telemetry()
    return out
