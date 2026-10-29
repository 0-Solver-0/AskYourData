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
     des candidates ; on garde ses meilleures correspondances ;
  3. tables retenues : la première du sujet, plus celles qui portent une colonne retenue ;
  4. une colonne retenue qui est une clé étrangère amène sa table cible (pour les noms) ;
     une table cible sans colonne de nom suit sa clé obligatoire vers une table qui en a
     (res_users → partner_id → res_partner) ;
  5. jointures entre tables retenues (graphe des clés étrangères), valeurs citées ;
  6. schéma minimal : clé primaire, colonnes retenues, clés de jointure, colonne de nom.
"""

from __future__ import annotations

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
)

NAME_COLUMNS = ("name", "display_name", "complete_name")
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
    subject_bonus: float = 0.05      # à égalité, une colonne de la table du sujet passe devant
    token_budget: int = 3000


def _usable(col: dict) -> bool:
    return (not col.get("is_pk") and col["column_name"] not in NOISE_COLUMNS
            and col["column_name"] not in TECHNICAL_FK_COLUMNS)


def _name_column(columns: list[dict]) -> Optional[str]:
    names = {c["column_name"] for c in columns}
    return next((n for n in NAME_COLUMNS if n in names), None)


def plan_structured(sq: StructuredQuestion, table_sims: dict[str, float],
                    column_sims: dict[tuple[str, str], list[float]], tables_by_name: dict[str, dict],
                    columns_by_table: dict[str, list[dict]], relations: list[dict],
                    params: StructuredParams, value_matches: Optional[list[ValueMatch]] = None) -> dict:
    """
    `table_sims` : {table: similarité avec le sujet} ;
    `column_sims` : {(table, colonne): [similarité avec chaque morceau de sq.pieces()]}.
    """
    pieces = sq.pieces()
    ranking = sorted((t for t in table_sims if t in tables_by_name), key=lambda t: table_sims[t], reverse=True)
    candidates = ranking[:params.candidate_tables]
    if not candidates:
        return {"tables": [], "schema_ddl": "", "token_estimate": 0, "relations": [], "pieces": [],
                "subject_ranking": []}

    # 2. Meilleures colonnes de chaque morceau, parmi les colonnes des tables candidates.
    #    Les colonnes de la table du sujet ont un léger avantage ; une correspondance suivante
    #    n'est gardée que si elle est presque aussi bonne que la meilleure.
    subject_set = set(ranking[:params.subject_tables])
    piece_matches = []
    for i, (role, text) in enumerate(pieces):
        scored = []
        for table in candidates:
            for col in columns_by_table.get(table, []):
                sims = column_sims.get((table, col["column_name"]))
                if sims and i < len(sims) and _usable(col):
                    sim = float(sims[i])
                    bonus = params.subject_bonus if table in subject_set else 0.0
                    scored.append((sim + bonus, sim, table, col["column_name"]))
        scored.sort(reverse=True)
        kept = [x for k, x in enumerate(scored[:params.matches_per_piece])
                if k == 0 or x[0] >= params.second_match_ratio * scored[0][0]]
        piece_matches.append({
            "role": role, "text": text,
            "matches": [{"table": t, "column": c, "similarity": round(sim, 3)} for _, sim, t, c in kept],
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
    for target in [t for t, why in reasons.items() if why.startswith("lookup:")]:
        cols = columns_by_table.get(target, [])
        if _name_column(cols):
            continue
        required = {c["column_name"] for c in cols if not c.get("is_nullable", True)}
        hops = [r for r in relations if r["from_table"] == target and r["from_column"] not in TECHNICAL_FK_COLUMNS
                and r["to_table"] in tables_by_name and _name_column(columns_by_table.get(r["to_table"], []))]
        hops.sort(key=lambda r: (r["to_table"] not in reasons, r["from_column"] not in required))
        if hops:
            used.append(hops[0])
            reasons.setdefault(hops[0]["to_table"], f"name:{target}.{hops[0]['from_column']}")

    # 5. Jointures entre tables principales, valeurs citées
    graph = build_graph(relations)
    for t, why in join_tables(graph, main, max_hops=2).items():
        reasons.setdefault(t, why)
    value_matches = [m for m in (value_matches or []) if m.table_name in tables_by_name]
    for m in value_matches:
        reasons.setdefault(m.table_name, f"value:{m.table_name}.{m.column_name}")
        matched_cols.setdefault(m.table_name, set()).add(m.column_name)
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
    }
