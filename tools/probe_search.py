"""
Sonde de la recherche, pour les tests à la main : un mot ou une expression → meilleures tables
(fiches) et meilleures colonnes, avec le score et le texte (libellé ou synonyme) qui l'a donné.
Sert à vérifier un mot, un synonyme ou une révision d'enrichissement sans lancer toute la chaîne.

Usage :
  python tools/probe_search.py "vendor"
  python tools/probe_search.py "vendor" "customer" "payment journal" --limit 5
  python tools/probe_search.py "vendor" --url http://askyourdata_api:5000 --account 1
Dépendance : requests.
"""

import argparse
import os
import sys

import requests


def show(url: str, account: int, text: str, limit: int) -> None:
    resp = requests.post(f"{url}/api/v1/schema/probe", json={"account_id": account, "text": text, "limit": limit},
                         timeout=60)
    data = resp.json()
    if resp.status_code != 200:
        print(f"✗ « {text} » : {data.get('error') or resp.status_code}")
        return
    print(f"\n━━ « {text} » ({data.get('embedder')}) " + "━" * max(0, 50 - len(text)))
    print("  Tables")
    for t in data["tables"]:
        print(f"    {t['similarity']:.3f}  {t['table_name']:<34} via « {t.get('matched') or 'fiche complète'} »")
    print("  Colonnes")
    for c in data["columns"]:
        name = f"{c['table_name']}.{c['column_name']}"
        print(f"    {c['similarity']:.3f}  {name:<44} via « {c.get('matched') or 'fiche complète'} »")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("texts", nargs="+")
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    for text in args.texts:
        show(args.url, args.account, text, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
