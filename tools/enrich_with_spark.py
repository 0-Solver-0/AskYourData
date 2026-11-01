"""
Enrichissement des fiches de tables et de colonnes par Muse Spark Open 30B (rôle « prep »).

Pour chaque table d'un module : l'export (GET /api/v1/schema/export) est envoyé au modèle avec les
règles de prompts/enrich_schema.md ; sa réponse remplit les blocs « enrich ». Le résultat est un
fichier par module, au format d'import, à RELIRE avant import — rien n'est écrit dans la base.
Ensuite, contrôle des synonymes de tables en double entre tables (un synonyme partagé rend le
classement aléatoire avec un score « meilleur vecteur »).

Usage (PowerShell, depuis le dossier du projet ; les clés viennent de .env) :
  docker run --rm --network nl2sql_net --env-file .env -v ${PWD}:/w -w /w askyourdata-api_service \
    python tools/enrich_with_spark.py --module sale --module purchase --url http://askyourdata_api:5000
Options :
  --tables sale_order,purchase_order   tables précises (au lieu des modules)
  --out enrichment/spark                dossier de sortie (ignoré par Git)
  --workers 4                           appels en parallèle
  --check FICHIER…                      seulement le contrôle des doublons sur des fichiers existants
Puis : relire, importer en simulation (dry_run=true), importer, réindexer si besoin.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

PROMPT_PATH = os.path.join(HERE, "..", "prompts", "enrich_schema.md")
COLUMNS_PER_CALL = 25        # au-delà, la réponse risque d'être tronquée (raisonnement compris)
MAX_TOKENS = 8000
DOMAINS = {"sales", "purchase", "accounting", "inventory", "crm", "hr", "product", "contact", "project", "other"}


# ── Message envoyé au modèle ─────────────────────────────────────────────────

def table_message(table: dict, columns: list[dict], columns_only: bool) -> str:
    """Faits de l'export, compacts : rien d'inutile pour le modèle (coût en tokens)."""
    def compact(d: dict) -> dict:
        return {k: v for k, v in d.items() if v not in (None, "", [], {})}

    facts = compact({
        "table_name": table["table_name"],
        "odoo_model": table.get("odoo_model"),
        "odoo_label": table.get("odoo_label"),
        "rows": table.get("row_count"),
        "referenced_by": (table.get("referenced_by") or [])[:12],
        "current": None if columns_only else compact(table.get("enrich") or {}),
        "columns": [compact({
            "name": c["column_name"], "type": c.get("odoo_type") or c.get("data_type"),
            "odoo_label": c.get("odoo_label"), "help": (c.get("odoo_help") or "")[:300] or None,
            "values": c.get("values"), "references": c.get("references"),
            "current": compact(c.get("enrich") or {}),
        }) for c in columns],
    })
    head = "Columns only (the table part was done in another message)." if columns_only else "Table and columns."
    return head + "\n" + json.dumps(facts, ensure_ascii=False)


# ── Réponse du modèle → blocs enrich ─────────────────────────────────────────

def _text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _synonyms(value: Any) -> Optional[str]:
    """Liste ou chaîne → « a, b, c » en minuscules, doublons retirés ; None si vide."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text.lower() not in out:
            out.append(text.lower())
    return ", ".join(out) or None


def apply_answer(table: dict, answer: dict, report: list[str]) -> None:
    """Remplit table["enrich"] et ceux des colonnes ; un champ absent ou vide reste inchangé (null)."""
    t = answer.get("table") if isinstance(answer, dict) else None
    if isinstance(t, dict):
        enrich = table.setdefault("enrich", {})
        for key in ("label", "description"):
            if _text(t.get(key)):
                enrich[key] = _text(t.get(key))
        if _synonyms(t.get("synonyms")):
            enrich["synonyms"] = _synonyms(t.get("synonyms"))
        domain = (_text(t.get("domain")) or "").lower()
        if domain in DOMAINS:
            enrich["domain"] = domain
    cols = answer.get("columns") if isinstance(answer, dict) else None
    by_name = {c["column_name"]: c for c in table.get("columns") or []}
    for name, values in (cols or {}).items():
        col = by_name.get(name)
        if col is None:
            report.append(f"{table['table_name']} : colonne inconnue « {name} » ignorée")
            continue
        if not isinstance(values, dict):
            continue
        enrich = col.setdefault("enrich", {})
        if _text(values.get("label")):
            enrich["label"] = _text(values.get("label"))
        if _synonyms(values.get("synonyms")):
            enrich["synonyms"] = _synonyms(values.get("synonyms"))
        if "notes" in values:
            enrich["notes"] = _text(values.get("notes")) or enrich.get("notes")
        if isinstance(values.get("always_include"), bool):
            enrich["always_include"] = values["always_include"]


# ── Contrôle des doublons ────────────────────────────────────────────────────

def _key(text: str) -> str:
    """Forme comparée : minuscules, singulier grossier (« bills » → « bill »)."""
    words = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


def duplicate_synonyms(tables: list[dict]) -> dict[str, list[str]]:
    """{synonyme ou libellé: [tables]} pour ceux portés par plusieurs tables."""
    owners: dict[str, set[str]] = {}
    for t in tables:
        enrich = t.get("enrich") or {}
        texts = [enrich.get("label") or ""] + (enrich.get("synonyms") or "").split(",")
        for text in texts:
            if text.strip():
                owners.setdefault(_key(text), set()).add(t["table_name"])
    return {k: sorted(v) for k, v in sorted(owners.items()) if len(v) > 1}


def print_duplicates(tables: list[dict]) -> int:
    dups = duplicate_synonyms(tables)
    print(f"\n── Synonymes ou libellés partagés entre tables : {len(dups)} ──")
    for text, names in dups.items():
        print(f"  « {text} » : {', '.join(names)}")
    if dups:
        print("  → garder chaque nom sur une seule table (la plus utilisée), sauf en-tête et lignes d'un même document.")
    return len(dups)


# ── Programme ────────────────────────────────────────────────────────────────

def enrich_table(client, prompt: str, table: dict, role: str, reasoning: Optional[str]) -> tuple[dict, dict]:
    """Une table (en plusieurs appels si elle a beaucoup de colonnes) → (table enrichie, télémétrie)."""
    from api_service.llm import LLMError, extract_json

    out = copy.deepcopy(table)
    columns = table.get("columns") or []
    chunks = [columns[i:i + COLUMNS_PER_CALL] for i in range(0, max(len(columns), 1), COLUMNS_PER_CALL)] or [[]]
    usage = {"calls": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "errors": []}
    for n, chunk in enumerate(chunks):
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": table_message(table, chunk, columns_only=n > 0)}]
        try:
            result = client.complete(role, messages, max_tokens=MAX_TOKENS, reasoning=reasoning)
            answer = extract_json(result.text)
        except (LLMError, ValueError) as exc:
            usage["errors"].append(f"{table['table_name']} (appel {n + 1}) : {exc}")
            continue
        tele = result.telemetry()
        usage["calls"] += 1
        usage["cost_usd"] += tele.get("cost_usd") or 0.0
        usage["tokens_in"] += tele.get("tokens_in") or 0
        usage["tokens_out"] += tele.get("tokens_out") or 0
        apply_answer(out, answer, usage["errors"])
    out["enriched_by"] = "spark-30b (à relire)"
    return out, usage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("ASKYOURDATA_URL", "http://localhost:5000"))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--tables", default=None)
    parser.add_argument("--out", default=os.path.join("enrichment", "spark"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--role", default="prep", help="rôle LLM utilisé (prep = Muse Spark Open 30B)")
    parser.add_argument("--reasoning", default=None)
    parser.add_argument("--check", nargs="+", default=None, help="fichiers déjà produits : contrôle seul")
    args = parser.parse_args()

    if args.check:
        tables = []
        for path in args.check:
            with open(path, encoding="utf-8") as fh:
                tables += json.load(fh).get("tables") or []
        print_duplicates(tables)
        return 0

    import requests

    from api_service.llm import LLMClient
    from api_service.prep import load_prompt

    if not args.module and not args.tables:
        parser.error("préciser --module (une ou plusieurs fois) ou --tables")
    prompt = load_prompt(PROMPT_PATH)
    client = LLMClient.from_env()
    jobs = [("tables", args.tables)] if args.tables else [("module", m) for m in args.module]
    os.makedirs(args.out, exist_ok=True)
    all_tables, total = [], {"calls": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    for kind, value in jobs:
        resp = requests.get(f"{args.url}/api/v1/schema/export", params={"account_id": args.account, kind: value},
                            timeout=120)
        if resp.status_code != 200:
            print(f"✗ export {kind}={value} : {resp.text[:200]}")
            continue
        data = resp.json()
        tables = data.get("tables") or []
        print(f"\n━━ {kind} {value} : {len(tables)} tables ━━")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(lambda t: enrich_table(client, prompt, t, args.role, args.reasoning), tables))
        data["tables"] = []
        for table, usage in results:
            data["tables"].append(table)
            for k in total:
                total[k] += usage[k]
            state = "✓" if not usage["errors"] else "!"
            print(f"  {state} {table['table_name']:<36} {usage['calls']} appel(s), {usage['cost_usd']:.4f} $")
            for err in usage["errors"]:
                print(f"      {err}")
        name = value if kind == "module" else "tables"
        path = os.path.join(args.out, f"{re.sub(r'[^a-z0-9_]+', '_', name.lower())}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"  → {path}")
        all_tables += data["tables"]

    print(f"\nTotal : {total['calls']} appels, entrée {total['tokens_in']} tokens, sortie {total['tokens_out']} tokens, "
          f"{total['cost_usd']:.4f} $")
    print_duplicates(all_tables)
    print("\nÀ relire avant import. Simulation : POST /api/v1/schema/import?account_id=1&dry_run=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
