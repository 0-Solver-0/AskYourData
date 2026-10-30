"""
Taux de bonnes réponses : chaque question passe par POST /api/v1/ask, et sa réponse est comparée
à la réponse de référence (SQL écrit à la main, exécuté avec le même rôle en lecture seule).

  --references-only : exécute seulement le SQL de référence et affiche les résultats,
                      pour les valider sur la base avant de s'en servir.
  --runs 3          : pose chaque question 3 fois (les réponses varient d'un passage à l'autre).

Usage (PowerShell, depuis le dossier du projet) :
  docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c
    "pip install -q requests pyyaml psycopg2-binary sqlglot==26.17.1 &&
     python benchmark/answer_eval.py --url http://askyourdata_api:5000 [--references-only] [--runs 3]"

Lit .env pour la connexion (SOURCE_DB_*, EXEC_DB_USER, EXEC_DB_PASSWORD).
Écrit benchmark/out/answers_<fichier>.md (ou references_<fichier>.md).
"""

import argparse
import os
import sys
import time

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "tools")]

from answer_compare import compare  # noqa: E402
from create_readonly_role import read_env  # noqa: E402

from api_service import executor  # noqa: E402


def table(columns, rows, limit=20):
    if not columns:
        return ["(aucune colonne)"]
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"| … {len(rows) - limit} lignes de plus |")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=os.path.join(HERE, "unseen_reference.yaml"))
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--references-only", action="store_true")
    parser.add_argument("--sql-reasoning", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    params = executor.exec_params(read_env())
    cases = yaml.safe_load(open(args.file, encoding="utf-8"))
    stem = os.path.splitext(os.path.basename(args.file))[0]
    out_path = args.out or os.path.join(HERE, "out", f"{'references' if args.references_only else 'answers'}_{stem}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    lines = [f"# {'Réponses de référence' if args.references_only else 'Bonnes réponses'} — "
             f"{time.strftime('%Y-%m-%d %H:%M')} — {args.file}", ""]

    good = total = 0
    times, costs = [], []
    per_question = []
    for case in cases:
        q = case["question"]
        ref = executor.execute(case["sql"], params)
        if not ref["ok"]:
            print(f"✗ {q}\n    SQL de référence en erreur : {ref['error']}")
            lines += [f"## {q}", "", f"SQL de référence en erreur : `{ref['error']}`", ""]
            continue
        if args.references_only:
            print(f"• {q}  ({ref['row_count']} lignes) — {case.get('definition', '')}")
            for row in ref["rows"][:10]:
                print("    " + " | ".join(str(v) for v in row))
            lines += [f"## {q}", "", f"_{case.get('definition', '')}_", "", "```sql", case["sql"].strip(), "```", "",
                      *table(ref["columns"], ref["rows"]), "", "- [ ] référence validée", ""]
            continue

        ok_runs = 0
        lines += [f"## {q}", "", f"_Référence : {case.get('definition', '')}_ — {ref['row_count']} lignes", ""]
        for run in range(1, args.runs + 1):
            body = {"question": q, "account_id": args.account}
            if args.sql_reasoning:
                body["reasoning"] = {"sql": args.sql_reasoning}
            try:
                data = requests.post(f"{args.url}/api/v1/ask", json=body, timeout=300).json()
            except Exception as exc:  # noqa: BLE001
                data = {"status": "error", "reason": str(exc)}
            total += 1
            times.append((data.get("timings_ms") or {}).get("total", 0) / 1000)
            costs.append(data.get("cost_usd") or 0.0)
            answer = data.get("answer")
            if data.get("status") == "ok" and answer:
                verdict = compare(ref["columns"], ref["rows"], answer["columns"], answer["rows"],
                                  ordered=bool(case.get("order")))
            else:
                verdict = {"match": False, "reason": f"{data.get('status')} : {data.get('reason')}"}
            ok_runs += verdict["match"]
            good += verdict["match"]
            mark = "✓" if verdict["match"] else "✗"
            label = f" (passage {run})" if args.runs > 1 else ""
            print(f"{mark} {q}{label}  [{times[-1]:.1f} s] — {verdict['reason']}")
            lines += [f"- {mark}{label} {verdict['reason']} · {data.get('status')} · {times[-1]:.1f} s · "
                      f"{costs[-1]:.4f} $"]
            if not verdict["match"]:
                if data.get("sql"):
                    lines += ["", "```sql", data["sql"], "```"]
                if answer:
                    lines += ["", *table(answer["columns"], answer["rows"], 10)]
                lines.append("")
        per_question.append((q, ok_runs))
        if not ok_runs or args.runs == 1:
            lines += ["", "Référence :", "", *table(ref["columns"], ref["rows"], 10)]
        lines.append("")

    if not args.references_only:
        n = len(times)
        print("\n── Bonnes réponses ──")
        print(f"Justes        : {good}/{total}" + (f" ({100 * good / total:.0f} %)" if total else ""))
        if args.runs > 1:
            for q, ok_runs in per_question:
                print(f"  {ok_runs}/{args.runs}  {q}")
        if n:
            print(f"Temps moyen   : {sum(times) / n:.1f} s (max {max(times):.1f} s)")
            print(f"Coût total    : {sum(costs):.4f} $ ({sum(costs) / n:.4f} $ par question)")
        lines[1:1] = ["", f"**Justes : {good}/{total}**", ""]
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"Détail        : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
