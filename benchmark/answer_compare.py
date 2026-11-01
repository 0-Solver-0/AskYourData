"""
Comparaison d'une réponse générée avec la réponse de référence (sans dépendance).

Règles :
- même nombre de lignes ;
- chaque colonne de référence doit correspondre à une colonne générée (noms ignorés : seules
  les valeurs comptent), les colonnes générées en plus sont permises ;
- valeurs normalisées : nombres arrondis à 2 décimales, textes sans espaces autour ;
- ordre des lignes comparé seulement si ordered=True (classements « top N »).
"""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Any

MAX_MAPPINGS = 5000


def norm(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, str):
        text = value.strip()
        try:
            return round(float(text), 2) if text and text.replace(".", "", 1).lstrip("-").isdigit() else text
        except ValueError:
            return text
    return str(value)


def compare(ref_columns: list, ref_rows: list, gen_columns: list, gen_rows: list, ordered: bool = False) -> dict:
    ref = [[norm(v) for v in r] for r in ref_rows]
    gen = [[norm(v) for v in r] for r in gen_rows]
    if len(ref) != len(gen):
        return {"match": False, "reason": f"{len(gen)} lignes au lieu de {len(ref)}"}
    if not ref:
        return {"match": True, "reason": "aucune ligne des deux côtés"}

    gen_values = [Counter(row[j] for row in gen) for j in range(len(gen_columns))]
    candidates = []
    for i, name in enumerate(ref_columns):
        values = Counter(row[i] for row in ref)
        found = [j for j, counter in enumerate(gen_values) if counter == values]
        if not found:
            return {"match": False, "reason": f"aucune colonne générée n'a les valeurs de « {name} »"}
        candidates.append(found)

    for n, mapping in enumerate(itertools.product(*candidates)):
        if n >= MAX_MAPPINGS:
            break
        if len(set(mapping)) != len(mapping):
            continue
        projected = [tuple(row[j] for j in mapping) for row in gen]
        expected = [tuple(row) for row in ref]
        if (projected == expected) if ordered else (Counter(projected) == Counter(expected)):
            return {"match": True, "reason": "mêmes valeurs",
                    "mapping": {ref_columns[i]: gen_columns[j] for i, j in enumerate(mapping)}}
    return {"match": False, "reason": "les colonnes existent mais les lignes ne correspondent pas"
            + (" (ou l'ordre diffère)" if ordered else "")}
