"""
schema_service/embedder.py
--------------------------
Embedder local paramétrable (sentence-transformers) : modèle, dimension, appareil, consignes.

Variables d'environnement :
  EMBEDDER_MODEL_PATH     dossier du modèle (défaut : MINILM_MODEL_PATH, puis all-MiniLM-L6-v2)
  EMBEDDER_DEVICE         auto | cpu | cuda          (auto : GPU s'il est disponible)
  EMBEDDER_DIM            dimension réduite (modèles « Matryoshka » comme Qwen3-Embedding) ; vide = native
  EMBEDDER_PROMPT_TABLES  consigne ajoutée à la question pour chercher les tables
  EMBEDDER_PROMPT_COLUMNS consigne ajoutée aux éléments de la question pour chercher les colonnes
                          (non définies : consignes par défaut du modèle, aucune pour MiniLM)
  EMBEDDER_FP16           true | false : demi-précision sur GPU (défaut true)
  EMBEDDER_BATCH_SIZE     taille des lots à l'indexation (défaut 32)

Les textes indexés (tables, colonnes) sont encodés sans consigne ; seules les requêtes en ont une.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("schema_service.embedder")

DEFAULT_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"

# Format attendu par Qwen3-Embedding : « Instruct: <tâche>\nQuery:<requête> »
QWEN3_PROMPTS = {
    "tables": ("Instruct: Given a business question about company data, "
               "retrieve the database tables needed to answer it\nQuery:"),
    "columns": ("Instruct: Given part of a business question, "
                "retrieve the database column that holds this information\nQuery:"),
}


def default_prompts(model_path: str) -> dict[str, str]:
    """Consignes par défaut selon le modèle : Qwen3-Embedding en attend une, MiniLM et bge-small non."""
    if "qwen3-embedding" in os.path.basename(model_path.rstrip("/\\")).lower():
        return dict(QWEN3_PROMPTS)
    return {"tables": "", "columns": ""}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class EmbedderConfig:
    model_path: str = DEFAULT_MODEL_PATH
    device: str = "auto"
    dim: Optional[int] = None
    prompt_tables: str = ""
    prompt_columns: str = ""
    fp16: bool = True
    batch_size: int = 32

    @classmethod
    def from_env(cls) -> "EmbedderConfig":
        path = os.getenv("EMBEDDER_MODEL_PATH") or os.getenv("MINILM_MODEL_PATH") or DEFAULT_MODEL_PATH
        prompts = default_prompts(path)
        dim = os.getenv("EMBEDDER_DIM", "").strip()
        device = (os.getenv("EMBEDDER_DEVICE") or "auto").strip().lower()
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError(f"EMBEDDER_DEVICE inconnu : {device} (auto | cpu | cuda)")
        return cls(
            model_path=path,
            device=device,
            dim=int(dim) if dim else None,
            # Variable définie (même vide) : elle remplace la consigne par défaut
            prompt_tables=os.environ.get("EMBEDDER_PROMPT_TABLES", prompts["tables"]),
            prompt_columns=os.environ.get("EMBEDDER_PROMPT_COLUMNS", prompts["columns"]),
            fp16=_env_bool("EMBEDDER_FP16", True),
            batch_size=int(os.getenv("EMBEDDER_BATCH_SIZE", "32")),
        )

    @property
    def model_id(self) -> str:
        """Identifiant enregistré avec l'index : un changement de modèle impose de réindexer."""
        name = os.path.basename(self.model_path.rstrip("/\\"))
        return f"{name}@{self.dim}" if self.dim else name


def resolve_device(requested: str, cuda_available: bool) -> str:
    if requested == "cpu":
        return "cpu"
    if cuda_available:
        return "cuda"
    if requested == "cuda":
        logger.warning("EMBEDDER_DEVICE=cuda mais aucun GPU n'est visible : passage sur CPU")
    return "cpu"


class Embedder:
    """Enveloppe de SentenceTransformer. `model` peut être injecté (tests)."""

    def __init__(self, cfg: EmbedderConfig, model: Any = None, device: Optional[str] = None):
        self.cfg = cfg
        self.model = model
        self.device = device or "cpu"

    def load(self) -> "Embedder":
        import torch  # import tardif : les tests n'en ont pas besoin
        from sentence_transformers import SentenceTransformer

        self.device = resolve_device(self.cfg.device, torch.cuda.is_available())
        logger.info("⏳ Chargement de l'embedder %s sur %s…", self.cfg.model_path, self.device)
        self.model = SentenceTransformer(self.cfg.model_path, device=self.device, truncate_dim=self.cfg.dim)
        if self.device == "cuda" and self.cfg.fp16:
            self.model.half()
        logger.info("✅ Embedder %s (%s, dimension %d)", self.cfg.model_id, self.device, self.dimension)
        return self

    @property
    def dimension(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    def _encode(self, texts: list[str], prompt: str = "") -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(texts, batch_size=self.cfg.batch_size, prompt=prompt or None,
                                    show_progress_bar=False)
        return [[float(x) for x in v] for v in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Textes indexés (tables, colonnes) : sans consigne."""
        return self._encode(texts)

    def embed_queries(self, texts: list[str], kind: str = "tables") -> list[list[float]]:
        """Requêtes : `kind` = tables (la question) ou columns (les éléments de la question)."""
        prompt = self.cfg.prompt_tables if kind == "tables" else self.cfg.prompt_columns
        return self._encode(texts, prompt)

    def embed_query(self, text: str, kind: str = "tables") -> list[float]:
        return self.embed_queries([text], kind)[0]

    def info(self) -> dict:
        return {
            "model": self.cfg.model_id,
            "model_path": self.cfg.model_path,
            "device": self.device,
            "dimension": self.dimension if self.model is not None else None,
            "query_prompts": bool(self.cfg.prompt_tables or self.cfg.prompt_columns),
        }
