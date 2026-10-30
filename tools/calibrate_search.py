"""
Calibrage des seuils de la recherche structurée pour l'embedder en place (ex. Contriever).

Chaque embedder a sa propre échelle de similarité : les seuils réglés pour un modèle ne valent
pas pour un autre. Ce script pose les questions de benchmark (découpage écrit à la main, tables
attendues connues) à POST /api/v1/schema/structured et affiche, d'après les vraies valeurs :
  - le sujet : rang et score de la table attendue, écart entre les premières tables ;
  - les morceaux : score de la meilleure colonne quand elle est dans une table attendue (« bon »)
    ou non (« mauvais ») → STRUCTURED_MIN_SIMILARITY ;
  - les regroupements : score de la fiche de table trouvée, bonne ou non → STRUCTURED_TABLE_PIECE_MIN ;
  - l'écart entre le 1er et le 2e sujet : s'il est faible, le classement du sujet est fragile.
Il propose des valeurs ; elles se règlent dans .env, puis `docker compose up -d schema_service`.

Usage (PowerShell, depuis le dossier du projet) :
  docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c
    "pip install -q requests pyyaml && python tools/calibrate_search.py --url http://askyourdata_api:5000"
Options : --file (plusieurs fois ; défaut : structured_questions.yaml et unseen_questions.yaml),
          --account 1, --verbose
"""

import argparse
import os
import statistics
import sys

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILES = [os.path.join(HERE, "..", "benchmark", f) for f in ("structured_questions.yaml",
                                                                    "unseen_questions.yaml")]


def expected_sets(expected) -> list[set[str]]:
    """[a, [b, c]] → [{a}, {b, c}] : chaque élément est une table, ou plusieurs tables acceptées."""
    return [set(e) if isinstance(e, list) else {e} for e in (expected or [])]


def quartiles(values: list[float]) -> str:
    if not values:
        return "—"
    v = sorted(values)
    q = statistics.quantiles(v, n=4) if len(v) >= 4 else [v[0], statistics.median(v), v[-1]]
    return f"n={len(v)}  min {v[0]:.3f}  q1 {q[0]:.3f}  médiane {statistics.median(v):.3f}  q3 {q[-1]:.3f}  max {v[-1]:.3f}"


def threshold_between(good: list[float], bad: list[float], default: float) -> float:
    """Seuil qui garde presque tous les bons et écarte le plus de mauvais : sous le 10e centile des bons."""
    if not good:
        return default
    g = sorted(good)
    low_good = g[max(0, int(len(g) * 0.1) - 1)] if len(g) >= 10 else g[0]
    candidates = [b for b in bad if b < low_good]
    best_bad = max(candidates) if candidates else None
    return round((low_good + best_bad) / 2, 2) if best_bad is not None else round(low_good - 0.02, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--file", action="append", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = []
    for path in args.file or DEFAULT_FILES:
        cases += [c for c in yaml.safe_load(open(path, encoding="utf-8")) if c.get("structured")]

    subject_ok, chosen_total, gaps, subject_scores, other_top = 0, 0, [], [], []
    piece_good, piece_bad, table_good, table_bad = [], [], [], []
    embedder = None
    for case in cases:
        body = {"account_id": args.account, "question": case["question"], "structured": case["structured"]}
        try:
            data = requests.post(f"{args.url}/api/v1/schema/structured", json=body, timeout=60).json()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {case['question']} : {exc}")
            continue
        debug = data.get("debug") or {}
        embedder = embedder or debug.get("embedder")
        groups = expected_sets(case.get("expected"))
        wanted = set().union(*groups) if groups else set()
        ranking = debug.get("subject_ranking") or []
        names = [r["table_name"] for r in ranking]
        subject = groups[0] if groups else set()
        rank = next((i + 1 for i, n in enumerate(names) if n in subject), None)
        # Sujet réellement retenu (règle « en-tête avant ses lignes » comprise), sans le juge
        chosen = debug.get("subject_table") or (names[0] if names else None)
        chosen_ok = chosen in subject
        if rank == 1:
            subject_ok += 1
        if chosen_ok:
            chosen_total += 1
        if len(ranking) >= 2:
            gaps.append(ranking[0]["similarity"] - ranking[1]["similarity"])
        if rank:
            subject_scores.append(ranking[rank - 1]["similarity"])
        other_top += [r["similarity"] for r in ranking[:5] if r["table_name"] not in wanted]

        lines = []
        for piece in debug.get("pieces") or []:
            if piece.get("matches"):
                m = piece["matches"][0]
                (piece_good if m["table"] in wanted else piece_bad).append(m["similarity"])
                lines.append(f"    {piece['role']:8} « {piece['text']} » → {m['table']}.{m['column']} "
                             f"{m['similarity']:.3f}{'' if m['table'] in wanted else '  (hors attendu)'}")
            tm = piece.get("table_match")
            if tm and tm.get("table"):
                (table_good if tm["table"] in wanted else table_bad).append(tm["similarity"])
                lines.append(f"             fiche de table → {tm['table']} {tm['similarity']:.3f}"
                             f"{'' if tm['table'] in wanted else '  (hors attendu)'}")
        mark = "✓" if chosen_ok else ("~" if rank else "✗")
        top = ", ".join(f"{r['table_name']} {r['similarity']:.3f}" for r in ranking[:3])
        by = debug.get("subject_by") or "vectors"
        print(f"{mark} {case['question']}\n    sujet attendu {sorted(subject)} : retenu {chosen} ({by}) ; "
              f"rang par score {rank or '>10'} ; tête : {top}")
        if args.verbose:
            print("\n".join(lines))

    n = len(cases)
    print(f"\n── Calibrage ({embedder}, {n} questions) ──")
    print(f"Sujet retenu juste      : {chosen_total}/{n}  (sans le juge ; règle « en-tête avant lignes » comprise)")
    print(f"Sujet 1er par score     : {subject_ok}/{n}")
    print(f"Score du bon sujet      : {quartiles(subject_scores)}")
    print(f"Autres tables du top 5  : {quartiles(other_top)}")
    print(f"Écart 1er – 2e          : {quartiles(gaps)}")
    print(f"Colonnes bonnes         : {quartiles(piece_good)}")
    print(f"Colonnes hors attendu   : {quartiles(piece_bad)}")
    print(f"Fiches de table bonnes  : {quartiles(table_good)}")
    print(f"Fiches hors attendu     : {quartiles(table_bad)}")
    print("\nValeurs proposées pour .env (à vérifier ensuite avec answer_eval) :")
    print(f"  STRUCTURED_MIN_SIMILARITY={threshold_between(piece_good, piece_bad, 0.6)}")
    print(f"  STRUCTURED_TABLE_PIECE_MIN={threshold_between(table_good, table_bad, 0.6)}")
    print("  STRUCTURED_SUBJECT_MARGIN : écart de colonnes (sujet d'abord), garder 0.06 sauf si les colonnes")
    print("  bonnes et hors attendu sont très proches (alors le réduire).")
    print("Écart 1er – 2e faible (médiane < 0,02) : le classement du sujet est fragile → le juge Spark aidera.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
