"""
Génération SQL de bout en bout, sans exécution (POST /api/v1/translate) : découpage par PREP
(ou découpage du fichier avec --use-structured), recherche, SQL.

Affiche pour chaque question le statut (sql, missing, cannot_answer…), les tables envoyées,
le SQL, le temps et le coût ; écrit un fichier Markdown à relire (benchmark/out/sql_<fichier>.md).
Avec --execute : POST /api/v1/ask, la requête est vérifiée puis exécutée en lecture seule ;
affiche le résumé, les premières lignes, les corrections éventuelles.

Usage :
  python benchmark/sql_eval.py --file benchmark/unseen_questions.yaml [--execute] [--use-structured]
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
    parser.add_argument("--execute", action="store_true", help="vérifier et exécuter (POST /api/v1/ask)")
    args = parser.parse_args()

    cases = yaml.safe_load(open(args.file, encoding="utf-8"))
    out_path = args.out or os.path.join(HERE, "out", f"{'ask' if args.execute else 'sql'}_{os.path.splitext(os.path.basename(args.file))[0]}.md")
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
            endpoint = "ask" if args.execute else "translate"
            data = requests.post(f"{args.url}/api/v1/{endpoint}", json=body, timeout=300).json()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {q}\n    erreur : {exc}")
            continue
        status = data.get("status") or "?"
        statuses[status] = statuses.get(status, 0) + 1
        total_s = (data.get("timings_ms") or {}).get("total", 0) / 1000
        times.append(total_s)
        costs.append(data.get("cost_usd") or 0.0)
        models = data.get("models") or {}
        mark = {"ok": "✓", "sql": "✓", "missing": "?", "cannot_answer": "•"}.get(status, "✗")
        retried = " ↻" if len(data.get("attempts") or []) > 1 else ""
        print(f"{mark}{retried} {q}  [{status}, {total_s:.1f} s, {data.get('cost_usd', 0):.4f} $, sql={models.get('sql')}]")
        print(f"    tables : {', '.join(data.get('tables_used') or [])}")
        if data.get("not_found"):
            print(f"    non trouvé par la recherche : {data['not_found']}")
        if data.get("sql"):
            print("    " + data["sql"].replace("\n", "\n    "))
        attempts = data.get("attempts") or []
        if len(attempts) > 1:
            print(f"    1er essai : manque {attempts[0].get('missing')} → recherche : " + "; ".join(
                f"{r['text']} → " + (", ".join(f"{x['table']}.{x['column']}" if x.get("column") else x["table"]
                                               for x in [r, *(r.get("also") or [])]) if r.get("found") else "non trouvé")
                for r in data.get("missing_report") or []))
        if data.get("missing"):
            print(f"    manque : {data['missing']}")
        if data.get("reason"):
            print(f"    raison : {data['reason']}")
        checks = data.get("checks") or []
        for n, chk in enumerate(checks[:-1] if status == "ok" else checks, start=1):
            print(f"    ✗ vérification {n} : {chk.get('error')}")
        answer = data.get("answer")
        if answer:
            print(f"    → {answer['summary']}  ({answer['row_count']} lignes)")
            for row in answer["rows"][:5]:
                print("      " + " | ".join(str(v) for v in row))

        lines += [f"## {q}", "",
                  f"- statut : **{status}** · {total_s:.1f} s · {data.get('cost_usd', 0):.4f} $ · "
                  f"PREP {data.get('prep_status')} · modèles {models}",
                  f"- découpage : `{data.get('structured_request')}`",
                  f"- tables : {', '.join(data.get('tables_used') or [])}"]
        if data.get("not_found"):
            lines.append(f"- non trouvé par la recherche : {data['not_found']}")
        if len(data.get("attempts") or []) > 1:
            lines.append(f"- 1er essai : manque {data['attempts'][0].get('missing')} ; recherche : "
                         + ", ".join(f"{r['text']} → {r.get('found') or 'non trouvé'} {r.get('table') or ''}"
                                     f"{'.' + r['column'] if r.get('column') else ''}"
                                     for r in data.get("missing_report") or []))
        if data.get("missing"):
            lines.append(f"- manque : {data['missing']}")
        if data.get("reason"):
            lines.append(f"- raison : {data['reason']}")
        for n, chk in enumerate(data.get("checks") or [], start=1):
            if chk.get("error"):
                lines.append(f"- vérification {n} refusée : `{chk['error']}`")
        if data.get("raw"):
            lines += ["", "Réponse brute du modèle :", "", "```", data["raw"], "```"]
        if data.get("sql"):
            lines += ["", "```sql", data["sql"], "```"]
        answer = data.get("answer")
        if answer:
            lines += ["", f"**Résultat** : {answer['summary']} ({answer['row_count']} lignes"
                      + (", tronqué" if answer["truncated"] else "") + ")", "",
                      "| " + " | ".join(answer["columns"]) + " |",
                      "|" + "---|" * len(answer["columns"])]
            lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in answer["rows"][:20]]
        if data.get("schema_ddl"):
            lines += ["", f"<details><summary>Schéma envoyé (~{data.get('token_estimate')} tokens)</summary>", "",
                      "```sql", data["schema_ddl"], "```", "", "</details>"]
        lines += ["", "- [ ] résultat juste" if args.execute else "- [ ] SQL juste", ""]

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
