"""
Clés d'accès au test public (voir api_service/access.py). À lancer sur le serveur, dans le conteneur :

  docker exec askyourdata_api python tools/keys.py create --name "Jury Meta" --group jury --quota 50
  docker exec askyourdata_api python tools/keys.py create --name "Karim (LinkedIn)"        # groupe public, 20 questions
  docker exec askyourdata_api python tools/keys.py disable --group public                  # seul le jury garde l'accès
  docker exec askyourdata_api python tools/keys.py enable  --group public                  # on rouvre
  docker exec askyourdata_api python tools/keys.py list
  docker exec askyourdata_api python tools/keys.py quota --name "Karim (LinkedIn)" --quota 30
  docker exec askyourdata_api python tools/keys.py revoke --name "Karim (LinkedIn)"
  docker exec askyourdata_api python tools/keys.py forget --name "Karim (LinkedIn)"        # efface ses questions
  docker exec askyourdata_api python tools/keys.py export > questions.json                # sans les noms

La clé n'est affichée qu'à sa création : seule son empreinte est enregistrée.
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from api_service import access  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--quota", type=int, default=None)
    c.add_argument("--expires-days", type=int, default=None)
    c.add_argument("--group", default="public", help="jury, public… (un groupe se coupe d'un coup)")
    for name in ("disable", "enable"):
        g = sub.add_parser(name)
        g.add_argument("--group")
        g.add_argument("--name")
    sub.add_parser("list")
    for name in ("revoke", "forget"):
        sub.add_parser(name).add_argument("--name", required=True)
    q = sub.add_parser("quota")
    q.add_argument("--name", required=True)
    q.add_argument("--quota", type=int, required=True)
    sub.add_parser("export")
    args = parser.parse_args()

    settings = access.Settings.from_env()
    conn = access.connect()
    try:
        if args.cmd == "create":
            quota = args.quota if args.quota is not None else settings.default_quota
            key, key_id = access.create_key(conn, args.name, quota, args.expires_days, args.group)
            print(f"Clé n° {key_id} pour « {args.name} » (groupe {args.group}, {quota} questions) :\n\n    {key}\n")
            print("Elle ne sera plus jamais affichée : envoie-la maintenant à la personne.")
        elif args.cmd == "list":
            rows = access.list_keys(conn)
            print(f"{'id':>3}  {'groupe':<8} {'nom':<32} {'clé':<8} {'questions':>10} {'coût $':>9}  état")
            for r in rows:
                expired = r["expires_at"] is not None and r["expires_at"] < datetime.now()
                state = ("révoquée" if r["revoked"] else "expirée" if expired else
                         "désactivée" if r["disabled"] else "active")
                print(f"{r['id']:>3}  {r['grp'][:8]:<8} {r['name'][:32]:<32} …{r['key_hint']:<7} {r['used']:>4}/{r['quota']:<5} "
                      f"{float(r['cost_usd']):>9.4f}  {state}")
        elif args.cmd in ("disable", "enable"):
            if not args.group and not args.name:
                parser.error("préciser --group ou --name")
            n = access.set_disabled(conn, args.cmd == "disable", group=args.group, name=args.name)
            print(f"{n} clé(s) {'désactivée(s)' if args.cmd == 'disable' else 'réactivée(s)'}.")
        elif args.cmd == "revoke":
            print(f"{access.revoke_key(conn, args.name)} clé(s) révoquée(s).")
        elif args.cmd == "quota":
            print(f"{access.set_quota(conn, args.name, args.quota)} clé(s) mise(s) à jour.")
        elif args.cmd == "forget":
            print(f"{access.forget(conn, args.name)} question(s) effacée(s) ; clé révoquée.")
        elif args.cmd == "export":
            json.dump(access.export_questions(conn), sys.stdout, ensure_ascii=False, indent=2, default=str)
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
