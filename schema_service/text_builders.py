"""
schema_service/text_builders.py
-------------------------------
Construction des textes encodés en vecteurs (tables et colonnes).

Fonctions pures, testables sans modèle ni base.
L'embedder tronque les textes longs (512 tokens pour Contriever) : les
informations les plus utiles (libellés, description) sont donc placées en tête.
"""

from __future__ import annotations

from typing import Iterable, Optional

# Colonnes techniques présentes sur presque toutes les tables Odoo : du bruit pour la recherche
TECHNICAL_COLUMNS = {
    "id", "create_uid", "create_date", "write_uid", "write_date",
    "message_main_attachment_id", "__last_update",
}

# Libellés présents sur la plupart des tables Odoo : ils rendent toutes les tables semblables
# et sont donc retirés du texte d'une table (ils restent indexés au niveau des colonnes).
GENERIC_COLUMN_LABELS = {
    "id", "company", "currency", "company currency", "sequence", "active", "color", "color index",
    "created by", "created on", "last updated by", "last updated on", "display name",
    "message main attachment", "notes", "note", "description",
}
# Nombre maximal de colonnes citées dans le texte d'une table : l'embedder tronque vers 256 tokens
MAX_TABLE_TEXT_COLUMNS = 15

MAX_SELECTION_VALUES = 20
MAX_HELP_CHARS = 200


def _clean(value: Optional[str]) -> str:
    return " ".join(str(value).split()) if value else ""


def humanize(name: str) -> str:
    """sale_order_line → sale order line"""
    return name.replace("_", " ").replace(".", " ").strip()


def format_selection(values: Iterable[tuple[str, Optional[str]]]) -> Optional[str]:
    """[("draft", "Quotation"), ("sale", "Sales Order")] → 'draft=Quotation; sale=Sales Order'"""
    parts = []
    for i, (value, label) in enumerate(values):
        if i >= MAX_SELECTION_VALUES:
            parts.append("…")
            break
        label = _clean(label)
        parts.append(f"{value}={label}" if label and label != value else str(value))
    return "; ".join(parts) or None


def column_display_label(col: dict) -> str:
    """Meilleur libellé disponible : manuel > Odoo > nom humanisé."""
    return _clean(col.get("label")) or _clean(col.get("odoo_label")) or humanize(col["column_name"])


def build_column_text(col: dict) -> str:
    """
    Fiche d'une colonne, rédigée comme un morceau de question (mesure, filtre, regroupement) :
    le sens d'abord (libellé, synonymes, notes, aide, valeurs possibles), le nom technique à la fin.
    """
    parts: list[str] = []
    for key in ("label", "odoo_label", "synonyms"):
        text = _clean(col.get(key))
        if text and text not in parts:
            parts.append(text)
    for key in ("notes", "odoo_help"):
        text = _clean(col.get(key))
        if text:
            parts.append(text[:MAX_HELP_CHARS])
    if col.get("selection_values"):
        parts.append(f"values: {col['selection_values']}")
    parts.append(f"column {col['column_name']}")
    return " | ".join(parts)


# Colonnes qui décrivent le mieux une table non enrichie, par type Odoo (du plus au moins parlant)
BUSINESS_TYPE_ORDER = {"selection": 0, "monetary": 1, "many2one": 2, "date": 3, "datetime": 3,
                       "float": 4, "integer": 5, "char": 6, "text": 7, "boolean": 8}


def build_table_text(table: dict, columns: list[dict], relation_labels: Iterable[str] = ()) -> str:
    """
    Fiche d'une table, rédigée comme le « sujet » d'une question : ce que la table contient.
    Libellé métier, description, synonymes, libellé Odoo, nom lisible, domaine.
    Table enrichie (description présente) : rien de plus, les colonnes ont leurs propres vecteurs.
    Table non enrichie : on ajoute ses colonnes métier les plus parlantes (champs à choix,
    montants, liens, dates…), au plus MAX_TABLE_TEXT_COLUMNS, jamais les colonnes techniques.
    """
    parts: list[str] = []
    for key in ("label", "description", "synonyms", "odoo_label"):
        text = _clean(table.get(key))
        if text and text not in parts:
            parts.append(text)
    parts.append(humanize(table["table_name"]))
    if _clean(table.get("domain")):
        parts.append(f"domain: {_clean(table['domain'])}")

    if not _clean(table.get("description")):
        business = [c for c in columns if c["column_name"] not in TECHNICAL_COLUMNS]
        business.sort(key=lambda c: BUSINESS_TYPE_ORDER.get(c.get("odoo_type") or "", 9))
        col_labels: list[str] = []
        for col in business:
            label = column_display_label(col)
            if label.lower() in GENERIC_COLUMN_LABELS or label in col_labels:
                continue
            col_labels.append(label)
            if len(col_labels) >= MAX_TABLE_TEXT_COLUMNS:
                break
        if col_labels:
            parts.append("contains: " + ", ".join(col_labels))

    rel = [_clean(x) for x in relation_labels if _clean(x)]
    if rel:
        parts.append("relations: " + ", ".join(rel))
    return " | ".join(parts)


# ── Plusieurs vecteurs par table et par colonne ──────────────────────────────
# Une fiche qui mêle plusieurs sens donne un vecteur moyen : une carte plus étroite d'une autre
# table passe devant. Une table ou une colonne enrichie est donc encodée en plusieurs textes
# courts — son libellé, puis chaque synonyme — et son score est le meilleur d'entre eux.
# La description, les notes, l'aide Odoo et les valeurs ne sont pas encodées : elles servent au
# DDL envoyé au modèle SQL. Sans libellé ni synonymes, la fiche complète d'avant sert de secours.

VectorText = tuple[str, str]   # (source : label | synonym | card, texte)


def split_synonyms(value: Optional[str]) -> list[str]:
    """'sales order, quotation ,, quote' → ['sales order', 'quotation', 'quote'] (doublons retirés)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in (value or "").replace(";", ",").split(","):
        text = _clean(part)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _label_and_synonyms(label: str, synonyms: list[str]) -> list[VectorText]:
    texts: list[VectorText] = [("label", label)] if label else []
    seen = {label.lower()} if label else set()
    for s in synonyms:
        if s.lower() not in seen:
            seen.add(s.lower())
            texts.append(("synonym", s))
    return texts


def table_vector_texts(table: dict, columns: list[dict], relation_labels: Iterable[str] = ()) -> list[VectorText]:
    """
    Textes encodés pour une table. Enrichie (libellé ou synonymes) : le libellé métier, puis un
    texte par synonyme. Sinon : la fiche complète (libellé Odoo, nom, colonnes parlantes).
    """
    label = _clean(table.get("label"))
    synonyms = split_synonyms(table.get("synonyms"))
    if label or synonyms:
        return _label_and_synonyms(label, synonyms)
    return [("card", build_table_text(table, columns, relation_labels))]


def column_vector_texts(col: dict) -> list[VectorText]:
    """
    Textes encodés pour une colonne. Enrichie (libellé ou synonymes) : son libellé (manuel, sinon
    Odoo, sinon le nom lisible), puis un texte par synonyme. Sinon : la fiche complète.
    """
    synonyms = split_synonyms(col.get("synonyms"))
    if _clean(col.get("label")) or synonyms:
        return _label_and_synonyms(column_display_label(col), synonyms)
    return [("card", build_column_text(col))]
