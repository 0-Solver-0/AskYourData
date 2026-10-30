"""
schema_service/structured.py
----------------------------
Recherche de schéma à partir d'une question **découpée en morceaux de sens** :

    {
      "subject":  "customer invoices",                       → fiches de TABLES
      "measures": ["unpaid amount"],                         → fiches de COLONNES
      "filters":  ["customer invoices only", "posted", "not fully paid"],
      "group_by": ["customer", "salesperson"],
      "period":   null,                                      (ex. "invoice date in 2025")
      "order":    "unpaid amount desc"                       (pas de recherche : porte sur la mesure)
    }

Le découpage est écrit à la main pour l'instant ; il sera produit par le rôle PREP (Muse Spark).

Étapes (fonctions pures : les similarités sont calculées par le service et passées en entrée) :
  1. le sujet classe les tables ; les meilleures forment la liste des candidates ;
  2. chaque morceau (mesure, filtre, regroupement, période) est cherché parmi les colonnes
     des candidates dont le type convient au rôle (mesure → nombre, période → date…) ;
     la table du sujet passe d'abord : une colonne d'une autre table n'est prise que si elle
     bat nettement la meilleure colonne du sujet ; on garde ses meilleures correspondances ;
  3. tables retenues : la première du sujet, plus celles qui portent une colonne retenue ;
  4. une colonne retenue qui est une clé étrangère amène sa table cible (pour les noms) ;
     une table cible sans colonne de nom suit sa clé obligatoire vers une table qui en a
     (res_users → partner_id → res_partner) ;
  5. jointures entre tables retenues (graphe des clés étrangères) ; une valeur citée n'ajoute
     sa table que si celle-ci est reliée directement à une table retenue ;
  6. schéma minimal : clé primaire, colonnes retenues, colonnes « toujours envoyées » (enrichissement :
     statut, type de document…), clés de jointure, colonne de nom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .search import (
    NOISE_COLUMNS,
    TECHNICAL_FK_COLUMNS,
    ValueMatch,
    build_graph,
    estimate_tokens,
    join_tables,
    pick_join_relations,
    render_table,
    shortest_path,
)

NAME_COLUMNS = ("name", "display_name", "complete_name")

# Genre d'une colonne → rôles qui peuvent la prendre (filtre : tous les genres)
ROLE_KINDS = {
    "measure":  {"number"},
    "period":   {"date"},
    "group_by": {"relation", "text", "date", "boolean"},
    "filter":   {"number", "relation", "text", "date", "boolean"},
}
NUMBER_TYPES = {"monetary", "float", "integer"}
DATE_TYPES = {"date", "datetime"}
PIECE_ROLES = ("measures", "filters", "group_by", "period")


@dataclass
class StructuredQuestion:
    subject: str
    measures: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    period: Optional[str] = None
    order: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "StructuredQuestion":
        if not isinstance(data, dict):
            raise ValueError("'structured' doit être un objet")
        subject = " ".join(str(data.get("subject") or "").split())
        if not subject:
            raise ValueError("'structured.subject' obligatoire (ce que l'on cherche : « customer invoices »…)")

        def as_list(key: str) -> list[str]:
            value = data.get(key) or []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                raise ValueError(f"'structured.{key}' doit être une liste de textes")
            return [" ".join(str(v).split()) for v in value if str(v).strip()]

        period = " ".join(str(data.get("period") or "").split()) or None
        order = " ".join(str(data.get("order") or "").split()) or None
        return cls(subject, as_list("measures"), as_list("filters"), as_list("group_by"), period, order)

    def pieces(self) -> list[tuple[str, str]]:
        """Morceaux cherchés parmi les colonnes : [(rôle, texte)]."""
        out = [("measure", t) for t in self.measures]
        out += [("filter", t) for t in self.filters]
        out += [("group_by", t) for t in self.group_by]
        if self.period:
            out.append(("period", self.period))
        return out


@dataclass
class StructuredParams:
    candidate_tables: int = 6        # tables du sujet parmi lesquelles on cherche les colonnes
    subject_tables: int = 1          # premières tables du sujet toujours gardées
    matches_per_piece: int = 2       # correspondances gardées par morceau (un peu de marge)…
    second_match_ratio: float = 0.95  # … si elles atteignent ce ratio de la meilleure (sinon : bruit)
    subject_bonus: float = 0.0       # avantage ajouté aux colonnes du sujet pour le classement
    subject_margin: float = 0.06     # sujet d'abord : une autre table doit battre le sujet d'au moins ça
    check_types: bool = True         # le genre de la colonne doit convenir au rôle du morceau
    min_similarity: float = 0.6      # en dessous : morceau « non trouvé » (aucune colonne gardée)
    table_piece_min: float = 0.6     # un regroupement qui ressemble à ce point à une table l'ajoute…
    table_piece_hops: int = 4        # … si elle est à moins de N liens du sujet
    token_budget: int = 3000


# Colonnes techniques utiles pour un rôle précis (« new customers this month » → create_date)
ROLE_ALLOWED_TECHNICAL = {"period": {"create_date"}}


def _usable(col: dict, role: Optional[str] = None) -> bool:
    name = col["column_name"]
    if name in ROLE_ALLOWED_TECHNICAL.get(role or "", set()):
        return not col.get("is_pk")
    return not col.get("is_pk") and name not in NOISE_COLUMNS and name not in TECHNICAL_FK_COLUMNS


def column_kind(col: dict) -> str:
    """number, date, boolean, relation ou text — d'après le type Odoo, sinon le type SQL."""
    odoo_type = (col.get("odoo_type") or "").lower()
    data_type = (col.get("data_type") or "").lower()
    if col.get("is_fk") or odoo_type in ("many2one", "many2one_reference"):
        return "relation"
    if odoo_type in NUMBER_TYPES:
        return "number"
    if odoo_type in DATE_TYPES:
        return "date"
    if odoo_type == "boolean":
        return "boolean"
    if odoo_type:
        return "text"
    if data_type.startswith(("numeric", "double", "real", "integer", "bigint", "smallint")):
        return "number"
    if data_type.startswith(("date", "timestamp")):
        return "date"
    if data_type == "boolean":
        return "boolean"
    return "text"


def _name_column(columns: list[dict]) -> Optional[str]:
    names = {c["column_name"] for c in columns}
    return next((n for n in NAME_COLUMNS if n in names), None)


def _path_from_selected(graph: dict, selected: list[str], target: str, max_hops: int) -> Optional[list[str]]:
    """Plus court chemin vers `target` depuis la table retenue la plus proche."""
    best = None
    for start in selected:
        path = shortest_path(graph, start, target, max_hops)
        if path and (best is None or len(path) < len(best)):
            best = path
    return best


def neighbor_tables(selected: list[str], relations: list[dict], tables_by_name: dict[str, dict]) -> list[str]:
    """Tables retenues + tables reliées par une clé (dans un sens ou dans l'autre), clés techniques exclues."""
    chosen = set(selected)
    out = list(selected)
    for r in relations:
        if r["from_column"] in TECHNICAL_FK_COLUMNS:
            continue
        for a, b in ((r["from_table"], r["to_table"]), (r["to_table"], r["from_table"])):
            if a in chosen and b in tables_by_name and b not in out:
                out.append(b)
    return out


# Genre de colonne attendu d'après les mots du manque signalé par le modèle SQL (anglais) :
# « sale date of the order » → date ; « quantity sold », « unit price » → nombre. Les deux à la fois
# (« number of days late ») ou aucun : pas de contrainte.
_DATE_WORDS = re.compile(r"\b(date|dates|deadline|when)\b", re.I)
_NUMBER_WORDS = re.compile(r"\b(amounts?|quantit(y|ies)|qty|prices?|costs?|revenues?|values?|totals?|sum|average|"
                           r"count|number of|margins?|balance|spend|spending|rates?)\b", re.I)


def expected_kind(text: str) -> Optional[str]:
    """date, number ou None (rien d'évident) pour un mot manquant."""
    is_date, is_number = bool(_DATE_WORDS.search(text)), bool(_NUMBER_WORDS.search(text))
    if is_date == is_number:
        return None
    return "date" if is_date else "number"


def find_missing(phrases: list[str], selected: list[str], column_sims: dict[tuple[str, str], list[float]],
                 phrase_table_sims: list[dict[str, float]], tables_by_name: dict[str, dict],
                 columns_by_table: dict[str, list[dict]], relations: list[dict], params: StructuredParams,
                 max_new_tables: int = 4) -> tuple[dict, dict, list[dict]]:
    """
    Recherche complémentaire : chaque mot manquant (en mots métier) est cherché
      a. parmi les colonnes des tables retenues et de leurs voisines (`column_sims`, une similarité
         par mot) : une colonne au-dessus de min_similarity suffit ;
      b. sinon parmi les fiches de tables (`phrase_table_sims`) : une table au-dessus de
         table_piece_min, reliée à une table retenue en table_piece_hops liens au plus.
    Le genre de colonne doit convenir au mot : une date ne reçoit qu'une colonne de date, une quantité,
    un prix ou un montant qu'une colonne numérique (expected_kind) ; sans colonne du bon genre, on
    passe aux fiches de tables.
    Renvoie (extra_columns, extra_tables, rapport par mot) pour plan_structured.
    """
    graph = build_graph(relations)
    kinds = {(t, c["column_name"]): column_kind(c) for t, cols in columns_by_table.items() for c in cols}
    usable = {(t, c["column_name"]) for t, cols in columns_by_table.items() for c in cols if _usable(c, "period")}
    extra_columns: dict[tuple[str, str], str] = {}
    extra_tables: dict[str, str] = {}
    report = []
    new_tables: set[str] = set()
    for i, text in enumerate(phrases):
        want = expected_kind(text)
        scored = sorted(((float(sims[i]), t, c) for (t, c), sims in column_sims.items()
                         if i < len(sims) and (t, c) in usable and (want is None or kinds.get((t, c)) == want)),
                        reverse=True)
        # La meilleure colonne de la table du sujet, celle des autres tables retenues ET la meilleure
        # colonne tout court (si elles diffèrent) : « manager » → hr_employee.parent_id et
        # hr_department.manager_id ; le modèle SQL choisit d'après les libellés et les notes.
        # Ordre : table du sujet (première retenue), autres tables retenues, puis toutes ; 3 au plus.
        subject_table = selected[0] if selected else None
        in_subject = next((x for x in scored if x[1] == subject_table), None)
        in_selected = next((x for x in scored if x[1] in selected and x[1] != subject_table), None)
        picks = []
        for cand in (in_subject, in_selected):
            if cand and cand[0] >= params.min_similarity:
                picks.append(cand)
        if scored and scored[0] not in picks and scored[0][0] >= params.min_similarity and \
                (scored[0][1] in selected or scored[0][1] in new_tables or len(new_tables) < max_new_tables):
            picks.append(scored[0])
        if picks:
            for sim, t, c in picks:
                extra_columns[(t, c)] = text
                if t not in selected:
                    new_tables.add(t)
            sim, t, c = picks[0]
            entry = {"text": text, "found": "column", "table": t, "column": c, "similarity": round(sim, 3)}
            if len(picks) > 1:
                entry["also"] = [{"table": t2, "column": c2, "similarity": round(s2, 3)} for s2, t2, c2 in picks[1:]]
            report.append(entry)
            continue
        sims = phrase_table_sims[i] if i < len(phrase_table_sims) else {}
        ranked = sorted(((v, t) for t, v in sims.items() if t in tables_by_name), reverse=True)
        hit = None
        for sim, t in ranked:
            if sim < params.table_piece_min:
                break
            if t in selected or _path_from_selected(graph, selected, t, params.table_piece_hops):
                hit = (sim, t)
                break
        if hit and (hit[1] in selected or len(new_tables) < max_new_tables):
            sim, t = hit
            if t not in selected:
                extra_tables[t] = text
                new_tables.add(t)
            report.append({"text": text, "found": "table", "table": t, "column": None, "similarity": round(sim, 3)})
            continue
        best = scored[0] if scored else None
        report.append({"text": text, "found": None, "table": best[1] if best else None,
                       "column": best[2] if best else None,
                       "similarity": round(best[0], 3) if best else None})
    return extra_columns, extra_tables, report


def plan_structured(sq: StructuredQuestion, table_sims: dict[str, float],
                    column_sims: dict[tuple[str, str], list[float]], tables_by_name: dict[str, dict],
                    columns_by_table: dict[str, list[dict]], relations: list[dict],
                    params: StructuredParams, value_matches: Optional[list[ValueMatch]] = None,
                    piece_table_sims: Optional[list[dict[str, float]]] = None,
                    extra_columns: Optional[dict[tuple[str, str], str]] = None,
                    extra_tables: Optional[dict[str, str]] = None) -> dict:
    """
    `extra_columns` / `extra_tables` : ajouts de la recherche complémentaire (find_missing),
    {(table, colonne): mot manquant} et {table: mot manquant} ; rien n'est retiré du plan de base.
    `table_sims` : {table: similarité avec le sujet} ;
    `column_sims` : {(table, colonne): [similarité avec chaque morceau de sq.pieces()]} ;
    `piece_table_sims` : pour chaque morceau, {table: similarité du morceau avec la fiche de table}
    (calculé pour les regroupements seulement ; vide ailleurs).
    """
    pieces = sq.pieces()
    ranking = sorted((t for t in table_sims if t in tables_by_name), key=lambda t: table_sims[t], reverse=True)
    candidates = ranking[:params.candidate_tables]
    if not candidates:
        return {"tables": [], "schema_ddl": "", "token_estimate": 0, "relations": [], "pieces": [],
                "subject_ranking": []}

    # 2. Meilleures colonnes de chaque morceau, parmi les colonnes des tables candidates :
    #    - seules les colonnes dont le genre convient au rôle (s'il en existe au moins une) ;
    #    - sujet d'abord : si la table du sujet a une colonne presque aussi bonne que la meilleure
    #      (écart ≤ subject_margin), on ne garde que les colonnes du sujet ;
    #    - une correspondance suivante n'est gardée que si elle est presque aussi bonne.
    subject_set = set(ranking[:params.subject_tables])
    piece_matches = []
    for i, (role, text) in enumerate(pieces):
        scored = []
        for table in candidates:
            for col in columns_by_table.get(table, []):
                sims = column_sims.get((table, col["column_name"]))
                if sims and i < len(sims) and _usable(col, role):
                    sim = float(sims[i])
                    bonus = params.subject_bonus if table in subject_set else 0.0
                    scored.append((sim + bonus, sim, table, col["column_name"], column_kind(col)))
        type_checked = False
        if params.check_types:
            allowed = ROLE_KINDS.get(role)
            typed = [x for x in scored if allowed is None or x[4] in allowed]
            if typed and len(typed) < len(scored):
                scored, type_checked = typed, True
        scored.sort(reverse=True)
        subject_first = False
        in_subject = [x for x in scored if x[2] in subject_set]
        if in_subject and scored and in_subject[0][1] >= scored[0][1] - params.subject_margin:
            subject_first = len(in_subject) < len(scored)
            scored = in_subject
        not_found = not scored or scored[0][1] < params.min_similarity
        kept = [] if not_found else [x for k, x in enumerate(scored[:params.matches_per_piece])
                                     if k == 0 or x[0] >= params.second_match_ratio * scored[0][0]]
        piece_matches.append({
            "role": role, "text": text, "type_checked": type_checked, "subject_first": subject_first,
            "not_found": not_found,
            "matches": [{"table": t, "column": c, "similarity": round(sim, 3)} for _, sim, t, c, _ in kept],
        })

    # 3. Tables retenues : début du classement du sujet + tables qui portent une colonne retenue
    reasons: dict[str, str] = {}
    for t in ranking[:params.subject_tables]:
        reasons[t] = "subject"
    matched_cols: dict[str, set[str]] = {}
    for pm in piece_matches:
        for m in pm["matches"]:
            matched_cols.setdefault(m["table"], set()).add(m["column"])
            reasons.setdefault(m["table"], f"{pm['role']}:{m['column']}")
    # 3 bis. Un regroupement qui désigne une table (« product category » → product_category)
    #        ajoute cette table et le chemin depuis le sujet, même à plusieurs liens.
    graph = build_graph(relations)
    subject_table = ranking[0]
    fk_columns = {(r["from_table"], r["from_column"]) for r in relations}
    for i, pm in enumerate(piece_matches):
        sims = (piece_table_sims or [])[i] if piece_table_sims and i < len(piece_table_sims) else {}
        if pm["role"] != "group_by" or not sims:
            continue
        best = max((t for t in sims if t in tables_by_name), key=lambda t: sims[t], default=None)
        pm["table_match"] = {"table": best, "similarity": round(sims[best], 3)} if best else None
        if not best or sims[best] < params.table_piece_min or best in reasons:
            continue
        # Entité déjà résolue : la colonne trouvée est une clé étrangère du sujet
        # (sale_order.team_id → crm_team) ; on n'ajoute pas une seconde table voisine.
        top = pm["matches"][0] if pm["matches"] else None
        if top and top["table"] in subject_set and (top["table"], top["column"]) in fk_columns:
            continue
        path = shortest_path(graph, subject_table, best, params.table_piece_hops)
        if not path:
            continue
        reasons[best] = f"group_by-table:{pm['text']}"
        for t in path[1:-1]:
            reasons.setdefault(t, f"join:{subject_table}↔{best}")
    # 3 ter. Ajouts de la recherche complémentaire (mots manquants signalés par le modèle SQL)
    for (table, cname), text in (extra_columns or {}).items():
        if table not in tables_by_name:
            continue
        matched_cols.setdefault(table, set()).add(cname)
        reasons.setdefault(table, f"missing:{text}")
    for table, text in (extra_tables or {}).items():
        if table in reasons or table not in tables_by_name:
            continue
        path = _path_from_selected(graph, list(reasons), table, params.table_piece_hops)
        if not path:
            continue
        reasons[table] = f"missing-table:{text}"
        for t in path[1:-1]:
            reasons.setdefault(t, f"join:{path[0]}↔{table}")
    main = list(reasons)

    # 4. Clés étrangères retenues → tables cibles (noms), puis saut vers une table qui a un nom
    by_fk = {(r["from_table"], r["from_column"]): r for r in relations}
    used: list[dict] = []
    for table, cols in matched_cols.items():
        for cname in sorted(cols):
            r = by_fk.get((table, cname))
            if r and r["to_table"] in tables_by_name and r["to_table"] != table:
                used.append(r)
                reasons.setdefault(r["to_table"], f"lookup:{table}.{cname}")
    def add_name_hop(target: str) -> None:
        """Table sans colonne de nom : suivre sa clé (obligatoire de préférence) vers une table qui en a."""
        cols = columns_by_table.get(target, [])
        if _name_column(cols):
            return
        required = {c["column_name"] for c in cols if not c.get("is_nullable", True)}
        hops = [r for r in relations if r["from_table"] == target and r["from_column"] not in TECHNICAL_FK_COLUMNS
                and r["to_table"] in tables_by_name and _name_column(columns_by_table.get(r["to_table"], []))]
        # La clé obligatoire d'abord (res_users.partner_id), puis une table déjà retenue
        hops.sort(key=lambda r: (r["from_column"] not in required, r["to_table"] not in reasons))
        if hops:
            used.append(hops[0])
            reasons.setdefault(hops[0]["to_table"], f"name:{target}.{hops[0]['from_column']}")

    for target in [t for t, why in reasons.items() if why.startswith("lookup:")]:
        add_name_hop(target)

    # 5. Jointures entre tables principales, valeurs citées
    for t, why in join_tables(graph, main, max_hops=2).items():
        reasons.setdefault(t, why)
    # Une valeur citée annote une table retenue, ou ajoute une table reliée directement
    # (clé étrangère dans un sens ou dans l'autre) à une table déjà retenue ; sinon elle est écartée.
    selected_now = set(reasons)
    linked_to_selected = {r["to_table"] for r in relations if r["from_table"] in selected_now} | \
                         {r["from_table"] for r in relations if r["to_table"] in selected_now}
    ignored_values: list[ValueMatch] = []
    kept_values: list[ValueMatch] = []
    for m in (value_matches or []):
        if m.table_name not in tables_by_name:
            continue
        (kept_values if m.table_name in selected_now | linked_to_selected else ignored_values).append(m)
    value_matches = kept_values
    for m in value_matches:
        reasons.setdefault(m.table_name, f"value:{m.table_name}.{m.column_name}")
        matched_cols.setdefault(m.table_name, set()).add(m.column_name)
    # Toute table ajoutée pour relier ou regrouper (jointure, regroupement, valeur) doit aussi
    # pouvoir afficher un nom : res_users arrivée par une jointure → res_partner.
    for target in [t for t, why in reasons.items() if why.startswith(("join:", "group_by-table:", "value:"))]:
        add_name_hop(target)
    core = [t for t, why in reasons.items() if not why.startswith(("lookup:", "name:"))]
    used += [r for r in pick_join_relations(relations, set(core), columns_by_table) if r not in used]
    # Une table de valeur ou de nom doit être reliée : clé directe vers une table déjà retenue
    linked = {r["from_table"] for r in used} | {r["to_table"] for r in used}
    for t in reasons:
        if t not in linked and len(reasons) > 1:
            direct = pick_join_relations(relations, {t} | set(core), columns_by_table)
            used += [r for r in direct if t in (r["from_table"], r["to_table"]) and r not in used][:1]
    selected = set(reasons)
    used = [r for r in used if r["from_table"] in selected and r["to_table"] in selected]

    # 6. Colonnes : clé primaire, colonnes retenues, clés de jointure, colonne de nom
    join_cols: dict[str, set[str]] = {}
    for r in used:
        join_cols.setdefault(r["from_table"], set()).add(r["from_column"])
        join_cols.setdefault(r["to_table"], set()).add(r["to_column"])
    planned = []
    for table, why in reasons.items():
        cols = columns_by_table.get(table, [])
        name_col = _name_column(cols)
        keep = matched_cols.get(table, set()) | join_cols.get(table, set()) | ({name_col} if name_col else set())
        chosen = []
        for c in cols:
            if c.get("is_pk"):
                chosen.append((c, "primary_key"))
            elif c["column_name"] in matched_cols.get(table, set()):
                chosen.append((c, "matched"))
            elif c.get("always_include"):
                chosen.append((c, "always"))
            elif c["column_name"] in join_cols.get(table, set()):
                chosen.append((c, "join"))
            elif c["column_name"] in keep:
                chosen.append((c, "name"))
        planned.append((table, why, chosen))

    values_by_table: dict[str, list[ValueMatch]] = {}
    for m in value_matches:
        values_by_table.setdefault(m.table_name, []).append(m)
    ddl = "\n\n".join(
        render_table(tables_by_name[t], cols, used, selected,
                     [f"question mentions {m.column_name} = '{m.value}'" for m in values_by_table.get(t, [])])
        for t, _, cols in planned)

    return {
        "tables": [{"table_name": t, "reason": why,
                    "subject_similarity": round(table_sims.get(t, 0.0), 3),
                    "columns": [{"column_name": c["column_name"], "reason": r} for c, r in cols]}
                   for t, why, cols in planned],
        "schema_ddl": ddl,
        "token_estimate": estimate_tokens(ddl),
        "relations": used,
        "pieces": piece_matches,
        "subject_ranking": [{"table_name": t, "similarity": round(table_sims[t], 3)} for t in ranking[:10]],
        "value_matches": [{"table_name": m.table_name, "column_name": m.column_name, "value": m.value}
                          for m in value_matches],
        "value_matches_ignored": [{"table_name": m.table_name, "column_name": m.column_name, "value": m.value}
                                  for m in ignored_values],
    }
