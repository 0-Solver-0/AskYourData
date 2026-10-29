"""
Découpage des questions par le rôle PREP (POST /api/v1/structure), comparé au découpage écrit
à la main quand le fichier en contient un.

Pour chaque question : statut, découpage du modèle, écarts avec le découpage manuel, tokens,
coût, temps. Écrit un fichier YAML au même format que les fichiers d'entrée (question,
structured, expected) : il se passe tel quel à schema_search_eval.py pour mesurer la
recherche sur le découpage du modèle.

Usage :
  python benchmark/prep_eval.py --file benchmark/unseen_questions.yaml [--out benchmark/out/prep.yaml]
                                [--url http://localhost:5000] [--reasoning low]
Dépendances : requests, pyyaml.
"""

import argparse
import os
import sys
import time

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ("subject", "measures", "filters", "group_by", "period", "order")


def differences(model: dict, hand: dict) -> list[str]:
    out = []
    for key in KEYS:
        a, b = model.get(key), hand.get(key)
        if key in ("measures", "filters", "group_by"):
            a, b = sorted(a or []), sorted(b or [])
        if (a or None) != (b or None):
            out.append(f"{key}: {a!r} (main : {b!r})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--out", default=None, help="YAML écrit (défaut : benchmark/out/prep_<fichier>.yaml)")
    parser.add_argument("--reasoning", default=None)
    args = parser.parse_args()

    cases = yaml.safe_load(open(args.file, encoding="utf-8"))
    out_path = args.out or os.path.join(HERE, "out", f"prep_{os.path.basename(args.file)}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    written, statuses = [], {}
    tokens_in = tokens_out = tokens_cached = 0
    cost = 0.0
    times = []
    for case in cases:
        q = case["question"]
        t0 = time.time()
        try:
            resp = requests.post(f"{args.url}/api/v1/structure", timeout=180,
                                 json={"question": q, **({"reasoning": args.reasoning} if args.reasoning else {})})
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {q}\n    erreur : {exc}")
            continue
        times.append(time.time() - t0)
        status = data.get("status", "?")
        statuses[status] = statuses.get(status, 0) + 1
        info = data.get("llm") or {}
        tokens_in += info.get("tokens_in") or 0
        tokens_out += info.get("tokens_out") or 0
        tokens_cached += info.get("tokens_cached") or 0
        cost += info.get("cost_usd") or 0.0
        structured = data.get("structured") or {}
        mark = "✓" if status == "ok" else "•"
        print(f"{mark} {q}  [{status}, {info.get('provider', '?')}, {times[-1]:.1f} s, "
              f"{info.get('tokens_out', 0)} tokens en sortie]")
        if status != "ok" and data.get("reason"):
            print(f"    raison : {data['reason']}")
        print("    " + " | ".join(f"{k}={structured.get(k)!r}" for k in KEYS if structured.get(k)))
        if case.get("structured"):
            for d in differences(structured, case["structured"]):
                print(f"    ≠ {d}")
        for w in data.get("warnings") or []:
            print(f"    ! {w}")
        entry = {"question": q}
        if status in ("ok", "degraded"):          # refus : pas de découpage à rechercher
            entry["structured"] = {k: structured.get(k) for k in KEYS}
        if case.get("expected"):
            entry["expected"] = case["expected"]
        if status != "ok":
            entry["prep_status"] = status
        written.append(entry)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Découpage produit par PREP ({time.strftime('%Y-%m-%d %H:%M')}) à partir de {args.file}\n")
        yaml.safe_dump(written, fh, allow_unicode=True, sort_keys=False, width=120)

    n = len(times)
    print("\n── Résumé PREP ──")
    print("Statuts            : " + ", ".join(f"{k} {v}" for k, v in statuses.items()))
    if n:
        print(f"Temps moyen        : {sum(times) / n:.1f} s (max {max(times):.1f} s)")
        print(f"Tokens             : {tokens_in} en entrée (dont {tokens_cached} en cache), {tokens_out} en sortie")
        print(f"Coût total         : {cost:.4f} $ ({cost / n:.5f} $ par question)")
    print(f"Fichier écrit      : {out_path}")
    print(f"Recherche ensuite  : python benchmark/schema_search_eval.py --file {out_path} --verbose")
    return 0


if __name__ == "__main__":
    sys.exit(main())
