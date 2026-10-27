"""
schema_service/text_builders.py
-------------------------------
Construction des textes encodés en vecteurs (tables et colonnes).

Fonctions pures, testables sans modèle ni base.
L'embedder tronque les textes longs (~256 tokens pour all-MiniLM-L6-v2) : les
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
    """Texte d'une colonne : nom, libellés, synonymes, aide courte, valeurs possibles."""
    parts = [col["column_name"], humanize(col["column_name"])]
    for key in ("label", "odoo_label", "synonyms"):
        text = _clean(col.get(key))
        if text and text not in parts:
            parts.append(text)
    help_text = _clean(col.get("odoo_help"))
    if help_text:
        parts.append(help_text[:MAX_HELP_CHARS])
    if col.get("selection_values"):
        parts.append(f"values: {col['selection_values']}")
    return " | ".join(parts)


def build_table_text(table: dict, columns: list[dict], relation_labels: Iterable[str] = ()) -> str:
    """
    Texte d'une table, court et distinctif, du plus informatif au moins informatif :
    nom, modèle Odoo, libellés, synonymes, domaine, description, puis au plus
    MAX_TABLE_TEXT_COLUMNS colonnes métier (sans les libellés communs à toutes les tables).
    """
    parts: list[str] = [table["table_name"], humanize(table["table_name"])]
    for key in ("odoo_model", "label", "odoo_label", "synonyms", "domain", "description"):
        text = _clean(table.get(key))
        if text and text not in parts:
            parts.append(text)

    col_labels = []
    for col in columns:
        if col["column_name"] in TECHNICAL_COLUMNS:
            continue
        label = column_display_label(col)
        if label.lower() in GENERIC_COLUMN_LABELS or label in col_labels:
            continue
        col_labels.append(label)
        if len(col_labels) >= MAX_TABLE_TEXT_COLUMNS:
            break
    if col_labels:
        parts.append("columns: " + ", ".join(col_labels))

    rel = [_clean(x) for x in relation_labels if _clean(x)]
    if rel:
        parts.append("relations: " + ", ".join(rel))

    return " | ".join(parts)
