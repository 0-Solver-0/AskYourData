"""
Génération SQL de bout en bout, sans exécution (POST /api/v1/translate) : découpage par PREP
(ou découpage du fichier avec --use-structured), recherche, SQL.

Affiche pour chaque question le statut (sql, missing, cannot_answer…), les tables envoyées,
le SQL, le temps et le coût ; écrit un fichier Markdown à relire (benchmark/out/sql_<fichier>.md).
Le SQL n'est pas exécuté : la vérification et l'exécution viennent à l'incrément 4.

Usage :
  python benchmark/sql_eval.py --file benchmark/unseen_questions.yaml [--use-structured]
                               [--url http://localhost:5000] [--account 1] [--sql-reasoning medium]
Dépendances : requests, pyyaml.
"""

import argparse
import os
import sys
import time

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--file", required=True)
    parser.add_argument("--use-structured", action="store_true", help="découpage du fichier, PREP sauté")
    parser.add_argument("--sql-reasoning", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cases = yaml.safe_load(open(args.file, encoding="utf-8"))
    out_path = args.out or os.path.join(HERE, "out", f"sql_{os.path.splitext(os.path.basename(args.file))[0]}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    lines = [f"# SQL généré — {time.strftime('%Y-%m-%d %H:%M')} — {args.file}", ""]
    statuses: dict[str, int] = {}
    times, costs = [], []
    for case in cases:
        q = case["question"]
        body = {"question": q, "account_id": args.account}
        if args.use_structured and case.get("structured"):
            body["structured"] = case["structured"]
        if args.sql_reasoning:
            body["reasoning"] = {"sql": args.sql_reasoning}
        try:
            data = requests.post(f"{args.url}/api/v1/translate", json=body, timeout=300).json()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {q}\n    erreur : {exc}")
            continue
        status = data.get("status") or "?"
        statuses[status] = statuses.get(status, 0) + 1
        total_s = (data.get("timings_ms") or {}).get("total", 0) / 1000
        times.append(total_s)
        costs.append(data.get("cost_usd") or 0.0)
        models = data.get("models") or {}
        mark = {"sql": "✓", "missing": "?", "cannot_answer": "•"}.get(status, "✗")
        print(f"{mark} {q}  [{status}, {total_s:.1f} s, {data.get('cost_usd', 0):.4f} $, sql={models.get('sql')}]")
        print(f"    tables : {', '.join(data.get('tables_used') or [])}")
        if data.get("not_found"):
            print(f"    non trouvé par la recherche : {data['not_found']}")
        if data.get("sql"):
            print("    " + data["sql"].replace("\n", "\n    "))
        if data.get("missing"):
            print(f"    manque : {data['missing']}")
        if data.get("reason"):
            print(f"    raison : {data['reason']}")

        lines += [f"## {q}", "",
                  f"- statut : **{status}** · {total_s:.1f} s · {data.get('cost_usd', 0):.4f} $ · "
                  f"PREP {data.get('prep_status')} · modèles {models}",
                  f"- découpage : `{data.get('structured_request')}`",
                  f"- tables : {', '.join(data.get('tables_used') or [])}"]
        if data.get("not_found"):
            lines.append(f"- non trouvé par la recherche : {data['not_found']}")
        if data.get("missing"):
            lines.append(f"- manque : {data['missing']}")
        if data.get("reason"):
            lines.append(f"- raison : {data['reason']}")
        if data.get("sql"):
            lines += ["", "```sql", data["sql"], "```"]
        if data.get("schema_ddl"):
            lines += ["", f"<details><summary>Schéma envoyé (~{data.get('token_estimate')} tokens)</summary>", "",
                      "```sql", data["schema_ddl"], "```", "", "</details>"]
        lines += ["", "- [ ] SQL juste", ""]

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    n = len(times)
    print("\n── Résumé génération SQL ──")
    print("Statuts       : " + ", ".join(f"{k} {v}" for k, v in statuses.items()))
    if n:
        print(f"Temps moyen   : {sum(times) / n:.1f} s (max {max(times):.1f} s)")
        print(f"Coût total    : {sum(costs):.4f} $ ({sum(costs) / n:.4f} $ par question)")
    print(f"À relire      : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
