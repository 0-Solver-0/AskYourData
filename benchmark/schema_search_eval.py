"""
Évaluation de la recherche de schéma sur la vraie base (sans LLM, sans coût).

Pour chaque question de benchmark/schema_questions.yaml : appelle /api/v1/schema/debug,
vérifie que les tables attendues sont bien envoyées au modèle SQL, et affiche la taille du schéma.

Usage :
  python benchmark/schema_search_eval.py [--url http://localhost:5000] [--account 1] [--verbose]
Dépendances : requests, pyyaml.
"""

import argparse
import os
import sys

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def satisfied(requirement, selected: set) -> bool:
    if isinstance(requirement, list):
        return any(t in selected for t in requirement)
    return requirement in selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5000")
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--file", default=os.path.join(HERE, "schema_questions.yaml"))
    parser.add_argument("--verbose", action="store_true", help="affiche aussi le classement et les raisons")
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        cases = yaml.safe_load(fh)

    total_req = total_ok = full_hits = 0
    tokens = []
    for case in cases:
        resp = requests.post(f"{args.url}/api/v1/schema/debug",
                             json={"question": case["question"], "account_id": args.account}, timeout=60)
        if resp.status_code != 200:
            print(f"✗ {case['question']}\n    erreur {resp.status_code} : {resp.text[:200]}")
            continue
        data = resp.json()
        selected = {t["table_name"] for t in data["tables"]}
        missing = [r for r in case["expected"] if not satisfied(r, selected)]
        ok = len(case["expected"]) - len(missing)
        total_req += len(case["expected"])
        total_ok += ok
        full_hits += not missing
        tokens.append(data.get("token_estimate") or 0)

        mark = "✓" if not missing else "✗"
        print(f"{mark} {case['question']}  ({ok}/{len(case['expected'])}, ~{data.get('token_estimate')} tokens)")
        print("    retenues : " + ", ".join(f"{t['table_name']} [{t['reason']}]" for t in data["tables"]))
        if missing:
            print(f"    manquantes : {missing}")
        if args.verbose:
            print("    classement : " + ", ".join(f"{r['table_name']}={r['score']}" for r in data["debug"]["ranking"][:8]))

    if not tokens:
        return 1
    print("\n── Résumé ──")
    print(f"Questions entièrement couvertes : {full_hits}/{len(cases)}")
    print(f"Rappel des tables attendues     : {total_ok}/{total_req} ({100 * total_ok / max(total_req, 1):.0f} %)")
    print(f"Taille moyenne du schéma        : ~{sum(tokens) // len(tokens)} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
