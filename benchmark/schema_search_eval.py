"""
Évaluation de la recherche de schéma sur la vraie base (sans LLM, sans coût).

Pour chaque question de benchmark/schema_questions.yaml : appelle /api/v1/schema/debug,
vérifie que les tables attendues sont bien envoyées au modèle SQL (rappel), compte les tables
en trop (précision) et affiche la taille du schéma. Compare les modes de recherche.

Usage :
  python benchmark/schema_search_eval.py [--url http://localhost:5000] [--account 1]
                                         [--mode legacy|coverage|both] [--verbose]
Dépendances : requests, pyyaml.

Précision = tables attendues trouvées / tables envoyées. Les tables de jointure qui relient
deux tables attendues (ex. res_users → res_partner) comptent comme « en trop » : la mesure
est volontairement stricte, à comparer entre modes plutôt qu'à viser 100 %.
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


def useful_tables(expected: list, selected: set) -> set:
    """Tables envoyées qui répondent à une exigence (une alternative trouvée compte une fois)."""
    useful = set()
    for req in expected:
        options = req if isinstance(req, list) else [req]
        found = [t for t in options if t in selected]
        if found:
            useful.add(found[0])
    return useful


def run(mode: str, cases: list, args) -> dict:
    print(f"\n══ Mode {mode} ══")
    total_req = total_ok = full_hits = 0
    sent = useful_total = 0
    tokens, times = [], []
    for case in cases:
        body = {"question": case["question"], "account_id": args.account}
        if mode != "default":
            body["search_mode"] = mode
        resp = requests.post(f"{args.url}/api/v1/schema/debug", json=body, timeout=60)
        if resp.status_code != 200:
            print(f"✗ {case['question']}\n    erreur {resp.status_code} : {resp.text[:200]}")
            continue
        data = resp.json()
        selected = {t["table_name"] for t in data["tables"]}
        missing = [r for r in case["expected"] if not satisfied(r, selected)]
        useful = useful_tables(case["expected"], selected)
        extra = sorted(selected - useful)
        ok = len(case["expected"]) - len(missing)
        total_req += len(case["expected"])
        total_ok += ok
        full_hits += not missing
        sent += len(selected)
        useful_total += len(useful)
        tokens.append(data.get("token_estimate") or 0)
        times.append(data.get("elapsed_s") or 0)

        mark = "✓" if not missing else "✗"
        print(f"{mark} {case['question']}  (rappel {ok}/{len(case['expected'])}, "
              f"précision {len(useful)}/{len(selected)}, ~{data.get('token_estimate')} tokens)")
        print("    retenues : " + ", ".join(f"{t['table_name']} [{t['reason']}]" for t in data["tables"]))
        if missing:
            print(f"    manquantes : {missing}")
        if extra:
            print(f"    en trop    : {extra}")
        if args.verbose:
            dbg = data.get("debug", {})
            print("    classement : " + ", ".join(
                f"{r['table_name']}={r['score']}" + (f" (gain {r['gain']})" if r.get("gain") is not None else "")
                for r in dbg.get("ranking", [])[:8]))
            for t in dbg.get("tables", []):
                if t.get("elements"):
                    els = ", ".join(f"{e}→{m['column']}{'' if m['covered'] else ' (non couvert)'}"
                                    for e, m in t["elements"].items())
                    print(f"    {t['table_name']} : {els}")

    if not tokens:
        return {}
    summary = {
        "mode": mode,
        "full": f"{full_hits}/{len(cases)}",
        "recall": 100 * total_ok / max(total_req, 1),
        "precision": 100 * useful_total / max(sent, 1),
        "tables": sent / len(tokens),
        "tokens": sum(tokens) // len(tokens),
        "ms": 1000 * sum(times) / len(times),
    }
    print("── Résumé ──")
    print(f"Questions entièrement couvertes : {summary['full']}")
    print(f"Rappel des tables attendues     : {total_ok}/{total_req} ({summary['recall']:.0f} %)")
    print(f"Précision (tables utiles)       : {useful_total}/{sent} ({summary['precision']:.0f} %)")
    print(f"Tables envoyées par question    : {summary['tables']:.1f}")
    print(f"Taille moyenne du schéma        : ~{summary['tokens']} tokens")
    print(f"Temps moyen de recherche        : {summary['ms']:.0f} ms")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5000")
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--file", default=os.path.join(HERE, "schema_questions.yaml"))
    parser.add_argument("--mode", default="both", choices=["legacy", "coverage", "both", "default"],
                        help="mode de recherche ; « default » : celui du service (SEARCH_MODE)")
    parser.add_argument("--verbose", action="store_true", help="affiche aussi le classement et les éléments")
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        cases = yaml.safe_load(fh)

    modes = ["legacy", "coverage"] if args.mode == "both" else [args.mode]
    summaries = [s for s in (run(m, cases, args) for m in modes) if s]
    if not summaries:
        return 1
    if len(summaries) > 1:
        print("\n══ Comparaison ══")
        print(f"{'mode':<10}{'complètes':>11}{'rappel':>9}{'précision':>11}{'tables':>8}{'tokens':>8}{'ms':>7}")
        for s in summaries:
            print(f"{s['mode']:<10}{s['full']:>11}{s['recall']:>8.0f}%{s['precision']:>10.0f}%"
                  f"{s['tables']:>8.1f}{s['tokens']:>8}{s['ms']:>7.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
