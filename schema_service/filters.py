"""
schema_service/filters.py
-------------------------
Décide quelles tables et colonnes de la base source sont indexées.

Fonctions pures (aucun accès base, aucun modèle) : testables sans infrastructure.
La configuration vient de config/index_filters.yaml.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

DEFAULT_FILTERS_PATH = os.getenv("INDEX_FILTERS_PATH", "/app/config/index_filters.yaml")


@dataclass
class FilterConfig:
    include_tables: list[str] = field(default_factory=list)
    exclude_transient: bool = True
    exclude_unmapped: bool = True
    exclude_tables: list[str] = field(default_factory=list)
    exclude_prefixes: list[str] = field(default_factory=list)
    exclude_suffixes: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    exclude_column_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "FilterConfig":
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Clés inconnues dans la configuration des filtres : {sorted(unknown)}")
        cfg = cls(**{k: v for k, v in data.items() if v is not None})
        # Normalisation : tout en minuscules pour des comparaisons fiables
        for name in ("include_tables", "exclude_tables", "exclude_prefixes",
                     "exclude_suffixes", "exclude_patterns", "exclude_column_patterns"):
            setattr(cfg, name, [str(x).lower() for x in getattr(cfg, name) or []])
        return cfg


def load_filter_config(path: str = DEFAULT_FILTERS_PATH) -> FilterConfig:
    """Charge la configuration ; sans fichier, aucune exclusion par motif (seulement les défauts)."""
    if not os.path.isfile(path):
        return FilterConfig()
    with open(path, encoding="utf-8") as fh:
        return FilterConfig.from_dict(yaml.safe_load(fh))


@dataclass
class TableInfo:
    """Ce que l'on sait d'une table au moment du filtrage."""
    name: str
    odoo_model: Optional[str] = None      # ex. "sale.order" ; None si aucun modèle trouvé
    transient: bool = False               # modèle transitoire Odoo (wizard)


def decide_table(table: TableInfo, cfg: FilterConfig, has_odoo_metadata: bool) -> tuple[bool, Optional[str]]:
    """
    Retourne (garder, raison_exclusion).
    La raison est lisible et stable : elle est enregistrée pour la relecture des filtres.
    """
    name = table.name.lower()

    if name in cfg.include_tables:
        return True, None
    if cfg.exclude_transient and table.transient:
        return False, "transient_model"
    if name in cfg.exclude_tables:
        return False, "excluded_table"
    for prefix in cfg.exclude_prefixes:
        if name.startswith(prefix):
            return False, f"prefix:{prefix}"
    for suffix in cfg.exclude_suffixes:
        if name.endswith(suffix):
            return False, f"suffix:{suffix}"
    for pattern in cfg.exclude_patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return False, f"pattern:{pattern}"
    if cfg.exclude_unmapped and has_odoo_metadata and not table.odoo_model:
        return False, "no_odoo_model"
    return True, None


def keep_column(column_name: str, cfg: FilterConfig) -> bool:
    """Faux pour les colonnes sensibles (mots de passe, secrets, jetons…)."""
    name = column_name.lower()
    return not any(fnmatch.fnmatchcase(name, p) for p in cfg.exclude_column_patterns)


def table_name_for_model(model: str) -> str:
    """Convention Odoo : sale.order → sale_order."""
    return model.replace(".", "_")
