"""
schema_service/search.py
------------------------
Choix des tables et colonnes envoyées au modèle SQL, pour une question.

Fonctions pures (ni base, ni modèle) : les scores de similarité vectorielle sont
calculés ailleurs et passés en entrée. Tout est testable sans infrastructure.

Deux modes de choix des tables « graines » (SEARCH_MODE) :

  legacy   : score unique = vecteurs (table) + vecteurs (meilleure colonne, tout l'index)
             + correspondance de mots + bonus/malus de volume ; les 5 meilleurs scores.
  coverage : recherche en deux temps.
             1. tables : question brute ↔ vecteurs des tables seulement (+ mots du nom et
                des libellés de la table) → une dizaine de tables candidates ;
             2. colonnes, table par table : chaque élément de la question (« unpaid »,
                « salesperson »…) ↔ colonnes de la table candidate ;
             3. une correspondance pèse selon la rareté de la colonne dans l'index (IDF) :
                `payment_state` désigne une table, `partner_id` n'en désigne aucune ;
             4. sélection par couverture : la meilleure table, puis une autre seulement si
                elle couvre un élément de la question que les tables retenues ne couvrent pas.

Étapes communes ensuite :
  - tables de jointure : chemins les plus courts entre graines (graphe des clés étrangères)
  - tables de référence : cibles des clés étrangères citées par la question
    (ex. « customer » → sale_order.partner_id → res_partner)
  - valeurs citées (« Morocco » → res_country)
  - élagage des colonnes, schéma compact commenté, budget de tokens
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
    # Mode coverage
    element_matches: dict = field(default_factory=dict)   # élément → ElementMatch
    coverage_score: float = 0.0
    coverage_gain: Optional[float] = None


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
                   max_columns: int, top_similar: int = 4,
                   matched: Optional[set[str]] = None) -> list[tuple[dict, str]]:
    """
    Colonnes gardées pour une table, avec la raison, par priorité :
      1. clé primaire
      2. clés de jointure vers une autre table sélectionnée (une par table cible)
      3. colonnes citées par un mot de la question
         (mode coverage : `matched`, la meilleure colonne de chaque élément de la question)
      4. colonnes essentielles (nom, état, type…) puis montants et dates les plus proches
      5. colonnes citées par un synonyme, puis les plus proches de la question
         (mode coverage : seulement les plus proches, moins nombreuses)
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
        if matched is not None:
            # Mode coverage : un mot de la question n'importe que via la colonne qui lui correspond le
            # mieux. Sinon « invoice » garderait toutes les colonnes dont le libellé contient « Invoice ».
            if c["column_name"] in matched:
                add(c, "matched")
        elif column_terms(c) & original:
            add(c, "mentioned")
    for c in usable:
        if c["column_name"] in ESSENTIAL_COLUMNS:
            add(c, "essential")
    for c in [c for c in by_sim if _is_amount(c)][:3]:
        add(c, "essential_amount")
    for c in [c for c in by_sim if _is_date(c)][:2]:
        add(c, "essential_date")
    if matched is None:
        for c in by_sim:
            if column_terms(c) & expanded:
                add(c, "mentioned_synonym")
    for c in by_sim[:top_similar]:
        add(c, "similar")

    priority = {"primary_key": 0, "join": 1, "mentioned": 2, "matched": 2, "essential": 3, "essential_amount": 4,
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


# ── Recherche en deux temps (mode coverage) ──────────────────────────────────

# Étape 1 (tables seules) : vecteur de la table + mots du nom et des libellés de la table
W1_VECTOR = 0.6
W1_LEXICAL = 0.4
# Étape 2 : bonus d'une colonne dont le libellé, les synonymes ou les valeurs possibles contiennent l'élément
ELEMENT_LEXICAL_BONUS = 0.15
# Élément cité dans le nom ou le libellé de la table elle-même (« stage » → crm_stage) :
# force fixe, pondérée par la rareté du mot parmi les noms de tables
TABLE_NAME_MATCH_STRENGTH = 0.6


@dataclass
class CoverageInput:
    """Entrée du mode coverage, calculée par le service (embedder + pgvector)."""
    elements: list[str]                                  # éléments de la question, encodés un par un
    element_sims: dict[tuple[str, str], list[float]]     # (table, colonne) → similarité avec chaque élément
    candidate_tables: list[str] = field(default_factory=list)
    element_texts: list[str] = field(default_factory=list)    # textes encodés (élément + contexte)


@dataclass
class ElementMatch:
    element: str
    column: Optional[str]      # None : l'élément est dans le nom ou le libellé de la table
    strength: float            # similarité (+ bonus si le mot est dans le libellé)
    idf: float                 # rareté de la colonne dans l'index (0 : partout, 1 : unique)
    covered: bool = False      # l'élément est-il couvert par cette table ?

    @property
    def weight(self) -> float:
        return self.idf * self.strength if self.covered else 0.0


def question_elements(query: Query) -> list[str]:
    """Éléments de la question cherchés dans les colonnes : les mots utiles, sans les nombres."""
    return [t for t in query.tokens if not t.isdigit()]


def element_texts(question: str, elements: list[str], window: int = 2) -> list[str]:
    """
    Texte encodé pour chaque élément : l'élément avec les `window` mots qui l'entourent dans la
    question (mots vides compris). Un mot isolé ne veut presque rien dire pour un embedder :
    « stock » seul trouve `move_id`, « units in stock » trouve une quantité en stock.
    window = 0 : l'élément seul.
    """
    words = _TOKEN_RE.findall(str(question).lower())
    singular = [_singular(w) for w in words]
    out = []
    for element in elements:
        if window <= 0 or element not in singular:
            out.append(element)
            continue
        i = singular.index(element)
        out.append(" ".join(words[max(0, i - window): i + window + 1]))
    return out


def stage1_score(query: Query, candidates: list[TableCandidate]) -> list[TableCandidate]:
    """
    Étape 1 : la question brute comparée aux tables seulement. Les colonnes n'entrent pas ici :
    une colonne commune (Customer, Salesperson…) rendrait toutes les tables semblables.
    """
    for c in candidates:
        c.lexical_score, c.matched_terms = lexical_score(query, c.core_tokens, set())
        c.data_score = data_score(c.row_count)
        c.score = W1_VECTOR * c.vector_score + W1_LEXICAL * c.lexical_score + c.data_score
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def table_candidates(query: Query, candidates: list[TableCandidate], n: int) -> list[str]:
    """Les n meilleures tables de l'étape 1 : la liste large où chercher les colonnes."""
    return [c.table_name for c in stage1_score(query, candidates)[:n]]


def column_idf(columns_by_table: dict[str, list[dict]]) -> dict[str, float]:
    """
    Rareté de chaque nom de colonne dans l'index, entre 0 (présente dans toutes les tables)
    et ~1 (une seule table) : log((N+1)/(df+1)) / log(N+1).
    """
    n = len(columns_by_table)
    df: dict[str, int] = {}
    for cols in columns_by_table.values():
        for name in {c["column_name"] for c in cols}:
            df[name] = df.get(name, 0) + 1
    if n <= 1:
        return {name: 1.0 for name in df}
    return {name: math.log((n + 1) / (k + 1)) / math.log(n + 1) for name, k in df.items()}


def _element_terms(col: dict) -> set[str]:
    """Mots d'une colonne pour les éléments : libellé, synonymes et valeurs possibles (« Customer Invoice »)."""
    terms = column_terms(col)
    if col.get("selection_values"):
        terms |= set(tokenize(col["selection_values"], keep_stopwords=True))
    return terms


def table_name_idf(query: Query, elements: list[str], candidates: list[TableCandidate]) -> dict[str, float]:
    """Rareté de chaque élément parmi les noms et libellés de tables (« crm » : beaucoup, « stage » : peu)."""
    n = len(candidates)
    out = {}
    for element in elements:
        terms = query.expansions.get(element, {element})
        k = sum(1 for c in candidates if terms & c.core_tokens)
        out[element] = math.log((n + 1) / (k + 1)) / math.log(n + 1) if n > 1 else 1.0
    return out


def match_elements(query: Query, elements: list[str], table: str, core_tokens: set[str],
                   columns: list[dict], element_sims: dict[tuple[str, str], list[float]],
                   idf: dict[str, float], name_idf: Optional[dict[str, float]] = None) -> dict[str, ElementMatch]:
    """Étape 2 : pour une table candidate, la meilleure colonne de chaque élément de la question."""
    usable = [c for c in columns
              if c["column_name"] not in NOISE_COLUMNS and c["column_name"] not in TECHNICAL_FK_COLUMNS
              and not c.get("is_pk")]
    out: dict[str, ElementMatch] = {}
    for i, element in enumerate(elements):
        terms = query.expansions.get(element, {element})
        best: Optional[ElementMatch] = None
        for c in usable:
            sims = element_sims.get((table, c["column_name"]))
            strength = float(sims[i]) if sims and i < len(sims) else 0.0
            if terms & _element_terms(c):
                strength += ELEMENT_LEXICAL_BONUS
            if best is None or strength > best.strength:
                best = ElementMatch(element, c["column_name"], strength, idf.get(c["column_name"], 0.0))
        if terms & core_tokens:
            by_name = ElementMatch(element, None, TABLE_NAME_MATCH_STRENGTH, (name_idf or {}).get(element, 1.0))
            if best is None or by_name.idf * by_name.strength >= best.idf * best.strength:
                best = by_name
        if best is not None:
            out[element] = best
    return out


def select_by_coverage(query: Query, candidates: list[TableCandidate],
                       columns_by_table: dict[str, list[dict]], coverage: CoverageInput,
                       params: "PlanParams",
                       relations: Optional[list[dict]] = None) -> tuple[list[TableCandidate], list[TableCandidate]]:
    """
    Étapes 1 à 3 du mode coverage. Retourne (classement, graines).
    Une table est retenue si elle apporte un élément de la question que les graines déjà
    retenues ne couvrent pas (ou couvrent nettement moins bien), pondéré par la rareté.
    """
    ranked = stage1_score(query, candidates)
    pool = ranked[:params.candidate_tables]
    elements = coverage.elements
    idf = column_idf(columns_by_table)
    name_idf = table_name_idf(query, elements, candidates)

    for c in pool:
        c.element_matches = match_elements(query, elements, c.table_name, c.core_tokens,
                                           columns_by_table.get(c.table_name, []), coverage.element_sims,
                                           idf, name_idf)
    # Un élément est couvert par une colonne si sa correspondance atteint un plancher absolu et une
    # part de la meilleure correspondance de cet élément par une colonne parmi les candidates.
    # Un élément cité dans le nom de la table est toujours couvert (pondéré par sa rareté).
    for element in elements:
        best = max((c.element_matches[element].strength for c in pool
                    if element in c.element_matches and c.element_matches[element].column is not None),
                   default=0.0)
        for c in pool:
            m = c.element_matches.get(element)
            if m is not None:
                m.covered = (m.column is None
                             or m.strength >= max(params.min_element_strength, params.cover_ratio * best))
    # Score final : proximité de la table + couverture. Les mots du nom de la table ne comptent
    # qu'une fois, dans la couverture (l'étape 1 ne sert qu'à constituer la liste des candidates).
    for c in pool:
        c.coverage_score = sum(m.weight for m in c.element_matches.values()) / max(len(elements), 1)
        c.score = W1_VECTOR * c.vector_score + params.coverage_weight * c.coverage_score + c.data_score

    pool.sort(key=lambda c: c.score, reverse=True)
    if not pool:
        return ranked, []
    seeds = [pool[0]]
    pool[0].reason = "search"
    covered = {e: m.weight for e, m in pool[0].element_matches.items()}
    for c in pool[1:]:
        if len(seeds) >= params.seed_tables:
            break
        gain = sum(max(0.0, m.weight - covered.get(e, 0.0)) for e, m in c.element_matches.items())
        c.coverage_gain = gain
        if gain >= params.min_coverage_gain:
            c.reason = "search"
            seeds.append(c)
            for e, m in c.element_matches.items():
                covered[e] = max(covered.get(e, 0.0), m.weight)
    add_fact_tables(seeds, pool, ranked, columns_by_table, relations or [], params)
    rest = [c for c in ranked if c not in pool]
    return pool + rest, seeds


def business_columns(columns: list[dict]) -> list[dict]:
    """Colonnes « métier » d'une table : ni clé primaire, ni colonne technique."""
    return [c for c in columns
            if not c.get("is_pk") and c["column_name"] not in NOISE_COLUMNS
            and c["column_name"] not in TECHNICAL_FK_COLUMNS
            and c["column_name"] not in ("active", "sequence", "color", "display_name")]


def add_fact_tables(seeds: list[TableCandidate], pool: list[TableCandidate], ranked: list[TableCandidate],
                    columns_by_table: dict[str, list[dict]], relations: list[dict],
                    params: "PlanParams") -> None:
    """
    Une table de référence (peu de colonnes métier : raisons de perte, étapes, catégories…)
    ne permet pas de compter ni de classer à elle seule. Si une graine en est une et qu'aucune
    autre graine ne pointe vers elle, on ajoute la meilleure table remplie qui pointe vers elle
    par une clé étrangère. Ex. « Top lost reasons » → crm_lost_reason + crm_lead (lost_reason_id).
    """
    seed_names = {c.table_name for c in seeds}
    by_name = {c.table_name: c for c in ranked}
    order = {c.table_name: i for i, c in enumerate(pool + [c for c in ranked if c not in pool])}
    for seed in list(seeds):
        if len(business_columns(columns_by_table.get(seed.table_name, []))) > params.reference_max_columns:
            continue
        pointing = {r["from_table"] for r in relations
                    if r["to_table"] == seed.table_name and r["from_table"] != seed.table_name
                    and r["from_column"] not in TECHNICAL_FK_COLUMNS}
        if pointing & seed_names:
            continue
        facts = [by_name[t] for t in pointing if t in by_name and (by_name[t].row_count or 0) > 0]
        if not facts:
            continue
        fact = min(facts, key=lambda c: order.get(c.table_name, 10 ** 6))
        fact.reason = f"fact:{seed.table_name}"
        seeds.append(fact)
        seed_names.add(fact.table_name)


# ── Plan complet ─────────────────────────────────────────────────────────────

@dataclass
class PlanParams:
    seed_tables: int = 5             # graines retenues au maximum
    min_relative_score: float = 0.7  # legacy : une graine doit atteindre ce ratio du meilleur score (sauf les 2 premières)
    join_seeds: int = 3              # chemins de jointure cherchés entre les N meilleures graines seulement
    max_tables: int = 10
    max_columns: int = 15
    token_budget: int = 3000
    max_join_hops: int = 2           # au plus une table intermédiaire : évite les détours par res_users/res_partner
    # Mode coverage
    candidate_tables: int = 10       # tables candidates de l'étape 1 (liste large)
    coverage_weight: float = 0.6     # poids de la couverture dans le score final d'une table
    min_element_strength: float = 0.3  # plancher pour qu'une colonne couvre un élément
    cover_ratio: float = 0.8         # … et part minimale de la meilleure correspondance de l'élément
    min_coverage_gain: float = 0.15  # apport minimal (pondéré par la rareté) pour ajouter une graine
    element_context: int = 2         # mots de contexte de part et d'autre d'un élément encodé
    reference_max_columns: int = 4   # au plus N colonnes métier : table de référence (→ ajout de la table de faits)


def plan_schema(query: Query, candidates: list[TableCandidate], tables_by_name: dict[str, dict],
                columns_by_table: dict[str, list[dict]], relations: list[dict],
                params: PlanParams, value_matches: Optional[list[ValueMatch]] = None,
                coverage: Optional[CoverageInput] = None) -> dict:
    """
    Choisit tables et colonnes, construit le schéma compact et respecte le budget de tokens.
    `coverage` fourni → mode coverage (recherche en deux temps), sinon mode legacy.
    Retourne {"tables": [...], "schema_ddl": str, "token_estimate": int, "relations": [...]}.
    """
    if not candidates:
        return {"tables": [], "schema_ddl": "", "token_estimate": 0, "relations": []}

    if coverage is None:
        ranked = score_tables(query, candidates)
        best = ranked[0].score
        seeds: list[TableCandidate] = []
        for i, c in enumerate(ranked[:params.seed_tables]):
            if i < 2 or c.score >= params.min_relative_score * best:
                c.reason = "search"
                seeds.append(c)
    else:
        ranked, seeds = select_by_coverage(query, candidates, columns_by_table, coverage, params, relations)
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

    def matched_columns(name: str) -> Optional[set[str]]:
        if coverage is None:
            return None
        c = by_score.get(name)
        return {m.column for m in (c.element_matches.values() if c else []) if m.covered and m.column}

    max_cols = params.max_columns
    while True:
        planned = []
        for name, reason in order:
            forced = join_cols.get(name, set()) | {m.column_name for m in values_by_table.get(name, [])}
            cols = select_columns(name, columns_by_table.get(name, []), query, forced, max_cols,
                                  top_similar=4 if coverage is None else 2, matched=matched_columns(name))
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
            "elements": {
                e: {"column": m.column or "(table)", "strength": round(m.strength, 3), "idf": round(m.idf, 3),
                    "covered": m.covered}
                for e, m in c.element_matches.items()
            } if c and c.element_matches else None,
            "coverage": round(c.coverage_score, 3) if c and coverage is not None else None,
            "coverage_gain": round(c.coverage_gain, 3) if c and c.coverage_gain is not None else None,
            "row_count": tables_by_name[name].get("row_count"),
            "columns": [{"column_name": col["column_name"], "reason": why} for col, why in cols],
        })
    return {
        "tables": tables_out,
        "schema_ddl": ddl,
        "token_estimate": tokens,
        "relations": [r for r in used_relations if r["from_table"] in selected and r["to_table"] in selected],
        "mode": "legacy" if coverage is None else "coverage",
        "elements": coverage.elements if coverage is not None else None,
        "ranking": [{"table_name": c.table_name, "score": round(c.score, 3),
                     **({"coverage": round(c.coverage_score, 3),
                         "gain": None if c.coverage_gain is None else round(c.coverage_gain, 3)}
                        if coverage is not None and c.element_matches else {})}
                    for c in ranked[:15]],
        "value_matches": [{"table_name": m.table_name, "column_name": m.column_name, "value": m.value}
                          for m in value_matches],
    }
