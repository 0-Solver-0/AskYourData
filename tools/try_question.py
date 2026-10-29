"""
Teste une question à la main et affiche chaque étape, lisiblement :
question d'origine → découpage (PREP) → schéma envoyé au modèle SQL → SQL généré (ou ce qui manque).

Usage :
  python tools/try_question.py "Top 5 customers by revenue this year"
  python tools/try_question.py "..." --structured '{"subject": "sales orders", "measures": ["revenue"]}'
  python tools/try_question.py "..." --url http://askyourdata_api:5000 --account 1 --sql-reasoning medium
Dépendance : requests.
"""

import argparse
import json
import os
import sys

import requests


def section(title: str) -> None:
    print(f"\n━━ {title} " + "━" * max(0, 70 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--structured", default=None, help="découpage JSON fourni (PREP sauté)")
    parser.add_argument("--prep-reasoning", default=None)
    parser.add_argument("--sql-reasoning", default=None)
    args = parser.parse_args()

    body = {"question": args.question, "account_id": args.account}
    if args.structured:
        body["structured"] = json.loads(args.structured)
    reasoning = {k: v for k, v in (("prep", args.prep_reasoning), ("sql", args.sql_reasoning)) if v}
    if reasoning:
        body["reasoning"] = reasoning
    resp = requests.post(f"{args.url}/api/v1/translate", json=body, timeout=300)
    data = resp.json()
    if resp.status_code != 200:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 1

    section("1. Question d'origine")
    print(data.get("question"))

    section(f"2. Découpage (PREP : {data.get('prep_status')})")
    for key, value in (data.get("structured_request") or {}).items():
        if value:
            print(f"  {key:9} {value}")
    if data.get("not_found"):
        print(f"  non trouvé par la recherche : {data['not_found']}")

    attempts = data.get("attempts") or []
    for n, attempt in enumerate(attempts, start=1):
        label = "" if len(attempts) == 1 else f" — essai {n}"
        section(f"3. Schéma envoyé{label} ({len(attempt.get('tables') or [])} tables, "
                f"~{attempt.get('token_estimate')} tokens)")
        print("  tables : " + ", ".join(attempt.get("tables") or []))
        print()
        print(attempt.get("schema_ddl"))
        section(f"   Réponse du modèle SQL{label} : {attempt.get('status')} ({attempt.get('elapsed_ms')} ms)")
        if attempt.get("sql"):
            print(attempt["sql"])
        if attempt.get("missing"):
            print("Manque : " + json.dumps(attempt["missing"], ensure_ascii=False))
        if n == 1 and data.get("missing_report"):
            print("\nRecherche complémentaire :")
            for r in data["missing_report"]:
                where = f"{r.get('table')}.{r.get('column')}" if r.get("column") else r.get("table")
                print(f"  « {r['text']} » → {r.get('found') or 'NON TROUVÉ'} {where or ''} ({r.get('similarity')})")

    section(f"4. Résultat final : {data.get('status')}")
    if data.get("sql"):
        print(data["sql"])
    if data.get("missing"):
        print("Manque : " + json.dumps(data["missing"], ensure_ascii=False))
    if data.get("reason"):
        print("Raison : " + data["reason"])
    if data.get("raw"):
        print("Réponse brute : " + data["raw"])

    section("Modèles, temps, coût")
    for role, usage in (data.get("usage") or {}).items():
        print(f"  {role:4} {usage.get('model')}  entrée {usage.get('tokens_in')} (cache {usage.get('tokens_cached')}), "
              f"sortie {usage.get('tokens_out')} (raisonnement {usage.get('tokens_reasoning')}), "
              f"{usage.get('cost_usd', 0):.4f} $" + ("  [secours]" if usage.get("fallback_used") else ""))
    print("  temps : " + ", ".join(f"{k} {v} ms" for k, v in (data.get("timings_ms") or {}).items()))
    print(f"  coût total : {data.get('cost_usd', 0):.4f} $")
    return 0


if __name__ == "__main__":
    sys.exit(main())
