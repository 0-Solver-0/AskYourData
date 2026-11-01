"""
api_service/answer.py
---------------------
Réponse à l'utilisateur, construite par le code à partir des lignes de la base : aucun appel
de modèle, aucun chiffre écrit par un modèle. Le résumé est un gabarit rempli avec les
valeurs exactes du résultat.
"""

from __future__ import annotations

from typing import Any


def label(column: str) -> str:
    """total_revenue → Total revenue."""
    text = " ".join(str(column).replace("_", " ").split())
    return text[:1].upper() + text[1:]


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def summarize(columns: list[str], rows: list[list], truncated: bool = False) -> str:
    if not rows:
        return "No data matches this question."
    if len(rows) == 1 and len(columns) == 1:
        return f"{label(columns[0])}: {fmt(rows[0][0])}."
    if len(rows) == 1:
        return "1 result: " + ", ".join(f"{label(c)} {fmt(v)}" for c, v in zip(columns, rows[0])) + "."
    count = f"{len(rows):,} rows" + (" (first rows only, the result was cut)" if truncated else "")
    first = rows[0]
    numeric = [i for i, v in enumerate(first) if _is_number(v)]
    text = [i for i, v in enumerate(first) if isinstance(v, str)]
    if numeric and text:
        i_name, i_measure = text[0], numeric[-1] if len(numeric) == 1 else numeric[0]
        return (f"{count}. First: {fmt(first[i_name])} ({label(columns[i_measure])} "
                f"{fmt(first[i_measure])}).")
    return f"{count}."


def build_answer(result: dict) -> dict:
    columns, rows = result.get("columns") or [], result.get("rows") or []
    return {"columns": columns, "rows": rows, "row_count": len(rows),
            "truncated": bool(result.get("truncated")),
            "summary": summarize(columns, rows, bool(result.get("truncated")))}
