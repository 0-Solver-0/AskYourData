"""
schema_service/search.py
------------------------
Choix des tables et colonnes envoyées au modèle SQL, pour une question.

Fonctions pures (ni base, ni modèle) : les scores de similarité vectorielle sont
calculés ailleurs et passés en entrée. Tout est testable sans infrastructure.

Étapes :
  1. score de chaque table = vecteurs (table) + vecteurs (meilleure colonne)
     + correspondance de mots (avec vocabulaire métier) + bonus/malus de volume
  2. tables « graines » : les meilleurs scores
  3. tables de jointure : chemins les plus courts entre graines (graphe des clés étrangères)
  4. tables de référence : cibles des clés étrangères citées par la question
     (ex. « customer » → sale_order.partner_id → res_partner)
  5. valeurs citées (« Morocco » → res_country)
  6. élagage des colonnes, schéma compact commenté, budget de tokens
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import yaml

DEFAULT_SYNONYMS_PATH = os.getenv("SEARCH_SYNONYMS_PATH", "/app/config/search_synonyms.yaml")

# Poids du score final d'une table
W_TABLE_VECTOR = 0.45
W_COLUMN_VECTOR = 0.25
W_LEXICAL = 0.30
EMPTY_TABLE_PENALTY = 0.15
DATA_BONUS_MAX = 0.05

# Clés étrangères qui ne servent jamais à relier deux tables métier (elles relient tout à tout)
TECHNICAL_FK_COLUMNS = {
    "create_uid", "write_uid", "company_id", "currency_id", "company_currency_id",
    "message_main_attachment_id",
}
# Colonnes techniques jamais envoyées au modèle (sauf si la question les cite)
NOISE_COLUMNS = {
    "create_uid", "write_uid", "create_date", "write_date", "company_currency_id",
    "message_main_attachment_id", "sequence", "color", "__last_update",
}

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "by", "for", "to", "and", "or", "with", "per", "from", "at",
    "is", "are", "was", "were", "be", "what", "which", "who", "whom", "how", "many", "much", "list",
    "show", "give", "me", "our", "we", "us", "my", "all", "each", "every", "top", "most", "least",
    "this", "that", "these", "those", "than", "more", "less", "do", "does", "did", "have", "has",
    "it", "its", "their", "there", "as", "between", "over", "under", "into", "get", "find",
    "number", "count", "total", "average", "sum", "highest", "lowest", "best", "worst", "first",
    "last", "year", "month", "week", "day", "quarter", "today", "current", "per", "any",
}

# Mots courants qui sont aussi des valeurs fréquentes dans Odoo (étapes, types, équipes…)
COMMON_VALUE_WORDS = {
    "new", "won", "lost", "sale", "sales", "contact", "invoice", "draft", "done", "cancel", "open",
    "other", "service", "product", "stock", "general", "bank", "cash", "customer", "vendor", "partner",
    "unit", "units", "day", "days", "hour", "hours", "europe", "america", "asia", "africa", "oceania",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ── Valeurs citées dans la question ──────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Minuscules, sans accents ni ponctuation, espaces simples : « Côte d'Ivoire » → « cote d ivoire »."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


@dataclass
class ValueMatch:
    table_name: str
    column_name: str
    value: str


def match_values(question: str, values: list[dict], max_tables: int = 2) -> list[ValueMatch]:
    """
    Valeurs connues citées telles quelles dans la question (mot entier), les plus longues d'abord.
    Les valeurs d'un seul mot courant (« New », « Sales »…) sont ignorées : trop ambiguës.
    Au plus une valeur par table et `max_tables` tables.
    """
    q = f" {normalize_text(question)} "
    matches: list[ValueMatch] = []
    seen_tables: set[str] = set()
    for v in sorted(values, key=lambda v: len(v["normalized"]), reverse=True):
        norm = v["normalized"]
        if v["table_name"] in seen_tables:
            continue
        # Mot seul courant, au singulier comme au pluriel (« Customers » est un emplacement de stock)
        if " " not in norm and (norm in STOPWORDS or norm in COMMON_VALUE_WORDS
                                or _singular(norm) in COMMON_VALUE_WORDS):
            continue
        if f" {norm} " in q:
            matches.append(ValueMatch(v["table_name"], v["column_name"], v["value"]))
            seen_tables.add(v["table_name"])
            if len(matches) >= max_tables:
                break
    return matches


# ── Vocabulaire ──────────────────────────────────────────────────────────────

def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: Optional[str], keep_stopwords: bool = False) -> list[str]:
    """Minuscules, découpage, singulier simple ; les mots vides sont retirés par défaut."""
    if not text:
        return []
    words = _TOKEN_RE.findall(str(text).lower())
    if keep_stopwords:
        return [_singular(t) for t in words]
    # Mots vides retirés AVANT le singulier : sinon « this » devient « thi » et passe le filtre
    tokens = [_singular(t) for t in words if t not in STOPWORDS]
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def load_synonyms(path: str = DEFAULT_SYNONYMS_PATH) -> dict[str, set[str]]:
    """{terme: {termes ajoutés}} ; vide si le fichier n'existe pas."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    out: dict[str, set[str]] = {}
    for key, values in raw.items():
        expanded: set[str] = set()
        for v in values or []:
            expanded.update(tokenize(v, keep_stopwords=True))
        for k in tokenize(key, keep_stopwords=True):
            out.setdefault(k, set()).update(expanded - {k})
    return out


@dataclass
class Query:
    text: str
    tokens: list[str]                       # mots utiles de la question
    expansions: dict[str, set[str]]         # mot → {mot} ∪ synonymes

    @property
    def all_terms(self) -> set[str]:
        terms: set[str] = set()
        for v in self.expansions.values():
            terms |= v
        return terms

    def expanded_text(self) -> str:
        """Question + termes ajoutés : c'est ce texte qui est encodé en vecteur."""
        extra = sorted(self.all_terms - set(self.tokens))
        return f"{self.text} ({', '.join(extra)})" if extra else self.text


def build_query(question: str, synonyms: dict[str, set[str]]) -> Query:
    tokens = list(dict.fromkeys(tokenize(question)))
    return Query(
        text=question,
        tokens=tokens,
        expansions={t: {t} | synonyms.get(t, set()) for t in tokens},
    )


# ── Scores des tables ────────────────────────────────────────────────────────

@dataclass
class TableCandidate:
    id: int
    table_name: str
    row_count: Optional[int]
    core_tokens: set[str]          # nom, modèle, libellés, synonymes, domaine, description
    column_tokens: set[str]        # noms et libellés de colonnes
    vector_score: float            # similarité question ↔ table
    column_vector_score: float     # meilleure similarité question ↔ colonne de la table
    lexical_score: float = 0.0
    data_score: float = 0.0
    score: float = 0.0
    reason: str = ""
    matched_terms: list[str] = field(default_factory=list)


def table_core_tokens(table: dict) -> set[str]:
    tokens: set[str] = set()
    for key in ("table_name", "odoo_model", "label", "odoo_label", "synonyms", "domain", "description"):
        tokens.update(tokenize(table.get(key), keep_stopwords=True))
    return tokens


def column_terms(col: dict) -> set[str]:
    """
    Mots d'une colonne : son libellé (manuel ou Odoo) et ses synonymes ; le nom technique
    seulement en l'absence de libellé. Le nom technique trompe : `partner_id` vaut « Vendor »
    sur un bon de commande et « Customer » sur une commande client.
    """
    terms: set[str] = set()
    for key in ("label", "odoo_label", "synonyms"):
        terms.update(tokenize(col.get(key), keep_stopwords=True))
    if not terms:
        terms.update(tokenize(col["column_name"], keep_stopwords=True))
    return terms - {"id"}


def column_tokens(columns: list[dict]) -> set[str]:
    tokens: set[str] = set()
    for col in columns:
        if col["column_name"] not in NOISE_COLUMNS:
            tokens |= column_terms(col)
    return tokens


def lexical_score(query: Query, core: set[str], cols: set[str]) -> tuple[float, list[str]]:
    """
    Pour chaque mot utile de la question (ou l'un de ses synonymes) :
    1 s'il apparaît dans le nom/libellés de la table, 0,5 s'il n'apparaît que dans ses colonnes.
    Moyenne sur les mots de la question.
    """
    if not query.tokens:
        return 0.0, []
    total, matched = 0.0, []
    for token in query.tokens:
        terms = query.expansions[token]
        if terms & core:
            total += 1.0
            matched.append(token)
        elif terms & cols:
            total += 0.5
            matched.append(f"{token}(col)")
    return total / len(query.tokens), matched


def data_score(row_count: Optional[int]) -> float:
    if row_count is None:
        return 0.0
    if row_count == 0:
        return -EMPTY_TABLE_PENALTY
    return DATA_BONUS_MAX * min(1.0, math.log10(row_count + 1) / 3)


def score_tables(query: Query, candidates: list[TableCandidate]) -> list[TableCandidate]:
    for c in candidates:
        c.lexical_score, c.matched_terms = lexical_score(query, c.core_tokens, c.column_tokens)
        c.data_score = data_score(c.row_count)
        c.score = (W_TABLE_VECTOR * c.vector_score
                   + W_COLUMN_VECTOR * c.column_vector_score
                   + W_LEXICAL * c.lexical_score
                   + c.data_score)
    return sorted(candidates, key=lambda c: c.score, reverse=True)


# ── Graphe des clés étrangères ───────────────────────────────────────────────

def build_graph(relations: list[dict]) -> dict[str, dict[str, tuple[str, str, str]]]:
    """
    Graphe non orienté : graph[a][b] = (from_table, from_column, to_table) d'une relation a ↔ b.
    Les clés techniques (create_uid, company_id…) sont ignorées.
    """
    graph: dict[str, dict[str, tuple[str, str, str]]] = {}
    for r in relations:
        if r["from_column"] in TECHNICAL_FK_COLUMNS or r["from_table"] == r["to_table"]:
            continue
        edge = (r["from_table"], r["from_column"], r["to_table"])
        graph.setdefault(r["from_table"], {}).setdefault(r["to_table"], edge)
        graph.setdefault(r["to_table"], {}).setdefault(r["from_table"], edge)
    return graph


def shortest_path(graph: dict, start: str, goal: str, max_hops: int = 3) -> Optional[list[str]]:
    """Plus court chemin (nombre de liens) entre deux tables, ou None au-delà de max_hops."""
    if start == goal:
        return [start]
    queue, seen = deque([[start]]), {start}
    while queue:
        path = queue.popleft()
        if len(path) - 1 >= max_hops:
            continue
        for nxt in graph.get(path[-1], {}):
            if nxt in seen:
                continue
            if nxt == goal:
                return path + [nxt]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def join_tables(graph: dict, seeds: list[str], max_hops: int = 3) -> dict[str, str]:
    """Tables intermédiaires nécessaires pour relier les graines entre elles : {table: raison}."""
    added: dict[str, str] = {}
    for i, a in enumerate(seeds):
        for b in seeds[i + 1:]:
            path = shortest_path(graph, a, b, max_hops)
            if not path:
                continue
            for t in path[1:-1]:
                if t not in seeds and t not in added:
                    added[t] = f"join:{a}↔{b}"
    return added


def lookup_tables(query: Query, columns_by_table: dict[str, list[dict]],
                  relations: list[dict], seeds: list[str],
                  tables_by_name: Optional[dict[str, dict]] = None, max_lookups: int = 3) -> dict[str, str]:
    """
    Cibles des clés étrangères d'une graine dont le nom ou le libellé correspond à la question.
    Ex. « customers » → sale_order.partner_id (« Customer ») → res_partner.
    """
    targets = {(r["from_table"], r["from_column"]): r["to_table"] for r in relations}
    terms = query.all_terms
    added: dict[str, str] = {}
    for table in seeds:
        # Les mots qui décrivent déjà la table elle-même (ex. « sale » pour sale_order) ne comptent pas
        own = set(tokenize(table))
        for col in columns_by_table.get(table, []):
            if not col.get("is_fk") or col["column_name"] in TECHNICAL_FK_COLUMNS:
                continue
            # Libellé de la clé (« Customer », « Stage », « Salesperson ») : plus fiable que son nom technique
            col_terms = set(tokenize(col.get("label"))) | set(tokenize(col.get("odoo_label")))
            if not col_terms:
                col_terms = set(tokenize(col["column_name"]))
            col_terms -= {"id"} | own
            target = targets.get((table, col["column_name"]))
            if not target or target in seeds or target in added or not col_terms & terms:
                continue
            info = (tables_by_name or {}).get(target, {})
            if info.get("row_count") == 0:
                continue                       # table vide : rien à y chercher
            # La table cible doit correspondre au même mot de la question :
            # « customer » (customer, partner, contact…) → res_partner (« partner », « Contact »)
            if tables_by_name is not None:
                core = table_core_tokens(info)
                if not any(exp & col_terms and exp & core for exp in query.expansions.values()):
                    continue
            added[target] = f"lookup:{table}.{col['column_name']}"
            if len(added) >= max_lookups:
                return added
    return added


# ── Colonnes ─────────────────────────────────────────────────────────────────

def _is_date(col: dict) -> bool:
    return (col.get("odoo_type") in ("date", "datetime")
            or "date" in col.get("data_type", "") or "timestamp" in col.get("data_type", ""))


def _is_amount(col: dict) -> bool:
    name = col["column_name"]
    return (col.get("odoo_type") == "monetary"
            or name.startswith(("amount", "price_", "expected_revenue", "prorated_revenue"))
            or name in ("balance", "debit", "credit", "quantity", "product_uom_qty", "qty_available"))


# Colonnes souvent indispensables pour filtrer ou nommer (si elles existent)
ESSENTIAL_COLUMNS = ("name", "state", "stage_id", "type", "move_type", "payment_state", "invoice_status",
                     "probability", "amount_total", "date", "date_order", "invoice_date")


def select_columns(table: str, columns: list[dict], query: Query, join_columns: set[str],
                   max_columns: int, top_similar: int = 4) -> list[tuple[dict, str]]:
    """
    Colonnes gardées pour une table, avec la raison, par priorité :
      1. clé primaire
      2. clés de jointure vers une autre table sélectionnée (une par table cible)
      3. colonnes citées par un mot de la question
      4. colonnes essentielles (nom, état, type…) puis montants et dates les plus proches
      5. colonnes citées par un synonyme, puis les plus proches de la question
    Une clé étrangère vers une table non sélectionnée n'est jamais gardée : elle ne sert à rien au modèle.
    """
    original = set(query.tokens)
    expanded = query.all_terms
    usable = [c for c in columns
              if c["column_name"] not in NOISE_COLUMNS
              and (not c.get("is_fk") or c["column_name"] in join_columns)]
    by_sim = sorted(usable, key=lambda c: float(c.get("similarity") or 0), reverse=True)

    chosen: dict[str, str] = {}

    def add(col: dict, reason: str) -> None:
        if col["column_name"] not in chosen:
            chosen[col["column_name"]] = reason

    for c in usable:
        if c.get("is_pk"):
            add(c, "primary_key")
    for c in usable:
        if c["column_name"] in join_columns:
            add(c, "join")
    for c in usable:
        if column_terms(c) & original:
            add(c, "mentioned")
    for c in usable:
        if c["column_name"] in ESSENTIAL_COLUMNS:
            add(c, "essential")
    for c in [c for c in by_sim if _is_amount(c)][:3]:
        add(c, "essential_amount")
    for c in [c for c in by_sim if _is_date(c)][:2]:
        add(c, "essential_date")
    for c in by_sim:
        if column_terms(c) & expanded:
            add(c, "mentioned_synonym")
    for c in by_sim[:top_similar]:
        add(c, "similar")

    priority = {"primary_key": 0, "join": 1, "mentioned": 2, "essential": 3, "essential_amount": 4,
                "essential_date": 5, "mentioned_synonym": 6, "similar": 7}
    ranked = sorted(chosen.items(), key=lambda kv: priority[kv[1]])[:max_columns]
    keep = dict(ranked)
    return [(c, keep[c["column_name"]]) for c in columns if c["column_name"] in keep]


# ── Schéma compact ───────────────────────────────────────────────────────────

def column_comment(col: dict) -> str:
    parts = []
    label = col.get("label") or col.get("odoo_label")
    if label:
        parts.append(str(label))
    if col.get("synonyms"):
        parts.append(f"({col['synonyms']})")
    if col.get("selection_values"):
        parts.append(f"values: {col['selection_values']}")
    if col.get("data_type") == "jsonb" and (
            col["column_name"] in ("name", "description") or col.get("odoo_type") in ("char", "text", "html")):
        parts.append("translatable JSON: use ->>'en_US'")
    return " ".join(parts)


def render_table(table: dict, cols: list[tuple[dict, str]], relations: list[dict],
                 selected: set[str], value_notes: Optional[list[str]] = None) -> str:
    header = f"CREATE TABLE {table['table_name']} ("
    label = table.get("label") or table.get("odoo_label")
    desc = table.get("description")
    notes = "; ".join(value_notes or [])
    comment = " — ".join(x for x in (label, (desc or "")[:160], notes) if x)
    if comment:
        header += f"  -- {comment}"

    entries: list[tuple[str, str]] = []
    for col, _ in cols:
        line = f"    {col['column_name']} {col['data_type']}"
        if col.get("is_pk"):
            line += " PRIMARY KEY"
        if not col.get("is_nullable", True):
            line += " NOT NULL"
        entries.append((line, column_comment(col)))
    kept = {c["column_name"] for c, _ in cols}
    for r in relations:
        if r["from_table"] == table["table_name"] and r["to_table"] in selected and r["from_column"] in kept:
            entries.append((f"    FOREIGN KEY ({r['from_column']}) REFERENCES {r['to_table']}({r['to_column']})", ""))

    body = []
    for i, (line, comment) in enumerate(entries):
        sep = "," if i < len(entries) - 1 else ""
        body.append(f"{line}{sep}  -- {comment}" if comment else f"{line}{sep}")
    return "\n".join([header, *body, ");"])


def estimate_tokens(text: str) -> int:
    """Estimation grossière (≈ 4 caractères par token) : suffisante pour un budget."""
    return math.ceil(len(text) / 4)


def pick_join_relations(relations: list[dict], selected: set[str],
                        columns_by_table: dict[str, list[dict]]) -> list[dict]:
    """
    Une seule clé de jointure par couple de tables sélectionnées. Ex. sale_order → res_partner a
    partner_id, partner_invoice_id et partner_shipping_id : on garde partner_id. Règle : la colonne
    nommée d'après la table cible (`partner_id` pour res_partner, `order_id` pour sale_order),
    sinon la plus proche de la question.
    """
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in relations:
        if (r["from_table"] in selected and r["to_table"] in selected
                and r["from_table"] != r["to_table"] and r["from_column"] not in TECHNICAL_FK_COLUMNS):
            by_pair.setdefault((r["from_table"], r["to_table"]), []).append(r)

    sims = {(t, c["column_name"]): float(c.get("similarity") or 0)
            for t, cols in columns_by_table.items() for c in cols}
    picked = []
    for (src, dst), rels in by_pair.items():
        canonical = f"{dst.split('_')[-1]}_id"
        rels.sort(key=lambda r: (r["from_column"] != canonical, -sims.get((src, r["from_column"]), 0)))
        picked.append(rels[0])
    return picked


# ── Plan complet ─────────────────────────────────────────────────────────────

@dataclass
class PlanParams:
    seed_tables: int = 5             # graines retenues au maximum
    min_relative_score: float = 0.7  # une graine doit atteindre ce ratio du meilleur score (sauf les 2 premières)
    join_seeds: int = 3              # chemins de jointure cherchés entre les N meilleures graines seulement
    max_tables: int = 10
    max_columns: int = 15
    token_budget: int = 3000
    max_join_hops: int = 2           # au plus une table intermédiaire : évite les détours par res_users/res_partner


def plan_schema(query: Query, candidates: list[TableCandidate], tables_by_name: dict[str, dict],
                columns_by_table: dict[str, list[dict]], relations: list[dict],
                params: PlanParams, value_matches: Optional[list[ValueMatch]] = None) -> dict:
    """
    Choisit tables et colonnes, construit le schéma compact et respecte le budget de tokens.
    Retourne {"tables": [...], "schema_ddl": str, "token_estimate": int, "relations": [...]}.
    """
    if not candidates:
        return {"tables": [], "schema_ddl": "", "token_estimate": 0, "relations": []}

    ranked = score_tables(query, candidates)
    best = ranked[0].score
    seeds: list[TableCandidate] = []
    for i, c in enumerate(ranked[:params.seed_tables]):
        if i < 2 or c.score >= params.min_relative_score * best:
            c.reason = "search"
            seeds.append(c)
    seed_names = [c.table_name for c in seeds]

    graph = build_graph(relations)
    joins = {t: r for t, r in join_tables(graph, seed_names[:params.join_seeds], params.max_join_hops).items()
             if t not in seed_names}
    lookups = lookup_tables(query, columns_by_table, relations, seed_names, tables_by_name)

    value_matches = [m for m in (value_matches or []) if m.table_name in tables_by_name]
    values_by_table: dict[str, list[ValueMatch]] = {}
    for m in value_matches:
        values_by_table.setdefault(m.table_name, []).append(m)

    order: list[tuple[str, str]] = [(c.table_name, c.reason or "search") for c in seeds]
    order += [(t, r) for t, r in joins.items()]
    taken = set(seed_names) | set(joins)
    for m in value_matches:                     # une valeur citée (« Morocco ») amène sa table
        if m.table_name not in taken:
            order.append((m.table_name, f"value:{m.table_name}.{m.column_name}"))
            taken.add(m.table_name)
    order += [(t, r) for t, r in lookups.items() if t not in taken]
    order = [(t, r) for t, r in order if t in tables_by_name][:params.max_tables]
    selected = {t for t, _ in order}

    used_relations = pick_join_relations(relations, selected, columns_by_table)
    join_cols: dict[str, set[str]] = {t: set() for t in selected}
    for r in used_relations:
        join_cols[r["from_table"]].add(r["from_column"])
        join_cols[r["to_table"]].add(r["to_column"])

    by_score = {c.table_name: c for c in ranked}

    max_cols = params.max_columns
    while True:
        planned = []
        for name, reason in order:
            forced = join_cols.get(name, set()) | {m.column_name for m in values_by_table.get(name, [])}
            cols = select_columns(name, columns_by_table.get(name, []), query, forced, max_cols)
            planned.append((name, reason, cols))
        ddl = "\n\n".join(
            render_table(tables_by_name[n], cols, used_relations, selected,
                         [f"question mentions {m.column_name} = '{m.value}'" for m in values_by_table.get(n, [])])
            for n, _, cols in planned)
        tokens = estimate_tokens(ddl)
        if tokens <= params.token_budget:
            break
        if max_cols > 8:
            max_cols -= 2                       # d'abord moins de colonnes par table
            continue
        droppable = [i for i, (n, r) in enumerate(order) if r == "search" and i >= 2]
        droppable = droppable or [i for i, (n, r) in enumerate(order)
                                  if not r.startswith(("join", "value")) and i >= 1]
        if not droppable:
            break
        removed = order.pop(droppable[-1])[0]  # puis la table la moins bien classée
        selected.discard(removed)

    tables_out = []
    for name, reason, cols in planned:
        c = by_score.get(name)
        tables_out.append({
            "table_name": name,
            "reason": reason,
            "score": round(c.score, 3) if c else None,
            "scores": {
                "vector": round(c.vector_score, 3),
                "column_vector": round(c.column_vector_score, 3),
                "lexical": round(c.lexical_score, 3),
                "data": round(c.data_score, 3),
            } if c else None,
            "matched_terms": c.matched_terms if c else [],
            "row_count": tables_by_name[name].get("row_count"),
            "columns": [{"column_name": col["column_name"], "reason": why} for col, why in cols],
        })
    return {
        "tables": tables_out,
        "schema_ddl": ddl,
        "token_estimate": tokens,
        "relations": [r for r in used_relations if r["from_table"] in selected and r["to_table"] in selected],
        "ranking": [{"table_name": c.table_name, "score": round(c.score, 3)} for c in ranked[:15]],
        "value_matches": [{"table_name": m.table_name, "column_name": m.column_name, "value": m.value}
                          for m in value_matches],
    }
