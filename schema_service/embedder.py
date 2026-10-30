"""
schema_service/embedder.py
--------------------------
Embedder local paramétrable (sentence-transformers) : modèle, dimension, appareil, consignes.

Modèle par défaut : facebook/contriever-msmarco, modèle ouvert de Meta AI (règle du hackathon :
tout modèle appelé à l'exécution est un modèle Meta). Exécuté en local, sur CPU, dans schema_service ;
regroupement par moyenne des tokens (créé automatiquement par sentence-transformers), 768 dimensions.

Variables d'environnement :
  EMBEDDER_MODEL_PATH     dossier du modèle (défaut : /app/models/contriever-msmarco)
  EMBEDDER_DEVICE         auto | cpu | cuda          (auto : GPU s'il est disponible)
  EMBEDDER_DIM            dimension réduite pour les modèles qui l'acceptent ; vide = native
  EMBEDDER_PROMPT_TABLES  consigne ajoutée à la question pour chercher les tables (défaut : aucune)
  EMBEDDER_PROMPT_COLUMNS consigne ajoutée aux éléments de la question pour chercher les colonnes
  EMBEDDER_BACKEND        sentence_transformers (défaut) | drama
                          drama : API native de facebook/drama-base (Meta AI, dérivé de Llama 3.2) —
                          questions par encode_queries (préfixe « Query: » ajouté par le modèle),
                          fiches par encode_documents ; les consignes EMBEDDER_PROMPT_* sont ignorées
  EMBEDDER_TRUST_REMOTE_CODE  true pour un modèle livré avec son propre code Python (DRAMA) ; false par
                          défaut, car ce code est exécuté au chargement
  EMBEDDER_FP16           true | false : demi-précision sur GPU (défaut true)
  EMBEDDER_BATCH_SIZE     taille des lots à l'indexation (défaut 32)

Les textes indexés (tables, colonnes) sont encodés sans consigne ; seules les requêtes en ont une.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("schema_service.embedder")

DEFAULT_MODEL_PATH = "/app/models/contriever-msmarco"
BACKENDS = ("sentence_transformers", "drama")


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
    backend: str = "sentence_transformers"
    trust_remote_code: bool = False

    @classmethod
    def from_env(cls) -> "EmbedderConfig":
        path = os.getenv("EMBEDDER_MODEL_PATH") or DEFAULT_MODEL_PATH
        dim = os.getenv("EMBEDDER_DIM", "").strip()
        device = (os.getenv("EMBEDDER_DEVICE") or "auto").strip().lower()
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError(f"EMBEDDER_DEVICE inconnu : {device} (auto | cpu | cuda)")
        backend = (os.getenv("EMBEDDER_BACKEND") or "sentence_transformers").strip().lower()
        if backend not in BACKENDS:
            raise ValueError(f"EMBEDDER_BACKEND inconnu : {backend} ({' | '.join(BACKENDS)})")
        return cls(
            model_path=path,
            device=device,
            dim=int(dim) if dim else None,
            prompt_tables=os.environ.get("EMBEDDER_PROMPT_TABLES", ""),
            prompt_columns=os.environ.get("EMBEDDER_PROMPT_COLUMNS", ""),
            fp16=_env_bool("EMBEDDER_FP16", True),
            batch_size=int(os.getenv("EMBEDDER_BATCH_SIZE", "32")),
            backend=backend,
            trust_remote_code=_env_bool("EMBEDDER_TRUST_REMOTE_CODE", False),
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
    """
    Enveloppe de l'embedder : SentenceTransformer, ou API native de DRAMA (encode_queries /
    encode_documents). `model` et `tokenizer` peuvent être injectés (tests).
    """

    def __init__(self, cfg: EmbedderConfig, model: Any = None, device: Optional[str] = None,
                 tokenizer: Any = None):
        self.cfg = cfg
        self.model = model
        self.tokenizer = tokenizer
        self.device = device or "cpu"
        self._dimension: Optional[int] = None

    def load(self) -> "Embedder":
        import torch  # import tardif : les tests n'en ont pas besoin

        self.device = resolve_device(self.cfg.device, torch.cuda.is_available())
        logger.info("⏳ Chargement de l'embedder %s (%s) sur %s…", self.cfg.model_path, self.cfg.backend, self.device)
        if self.cfg.backend == "drama":
            from transformers import AutoModel, AutoTokenizer

            self.tokenizer = AutoTokenizer.from_pretrained(self.cfg.model_path)
            self.model = AutoModel.from_pretrained(self.cfg.model_path,
                                                   trust_remote_code=self.cfg.trust_remote_code)
            self.model = self.model.to(self.device).eval()
        else:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(self.cfg.model_path, device=self.device, truncate_dim=self.cfg.dim,
                                             trust_remote_code=self.cfg.trust_remote_code)
        if self.device == "cuda" and self.cfg.fp16:
            self.model.half()
        logger.info("✅ Embedder %s (%s, dimension %d)", self.cfg.model_id, self.device, self.dimension)
        return self

    @property
    def dimension(self) -> int:
        if self.cfg.backend == "drama":
            if self._dimension is None:
                self._dimension = len(self.embed_documents(["dimension"])[0])
            return self._dimension
        return int(self.model.get_sentence_embedding_dimension())

    def _drama(self, texts: list[str], queries: bool) -> list[list[float]]:
        try:
            import torch
            no_grad = torch.no_grad()
        except ImportError:          # tests sans torch (faux modèle)
            no_grad = contextlib.nullcontext()

        encode = self.model.encode_queries if queries else self.model.encode_documents
        out: list[list[float]] = []
        with no_grad:
            for i in range(0, len(texts), self.cfg.batch_size):
                vectors = encode(self.tokenizer, texts[i:i + self.cfg.batch_size], use_nested=False)
                for v in vectors:
                    values = [float(x) for x in v]
                    out.append(values[:self.cfg.dim] if self.cfg.dim else values)   # Matryoshka
        return out

    def _encode(self, texts: list[str], prompt: str = "") -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(texts, batch_size=self.cfg.batch_size, prompt=prompt or None,
                                    show_progress_bar=False)
        return [[float(x) for x in v] for v in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Textes indexés (tables, colonnes) : sans consigne."""
        if self.cfg.backend == "drama":
            return self._drama(texts, queries=False) if texts else []
        return self._encode(texts)

    def embed_queries(self, texts: list[str], kind: str = "tables") -> list[list[float]]:
        """Requêtes : `kind` = tables (la question) ou columns (les éléments de la question)."""
        if self.cfg.backend == "drama":
            return self._drama(texts, queries=True) if texts else []
        prompt = self.cfg.prompt_tables if kind == "tables" else self.cfg.prompt_columns
        return self._encode(texts, prompt)

    def embed_query(self, text: str, kind: str = "tables") -> list[float]:
        return self.embed_queries([text], kind)[0]

    def info(self) -> dict:
        return {
            "model": self.cfg.model_id,
            "model_path": self.cfg.model_path,
            "backend": self.cfg.backend,
            "device": self.device,
            "dimension": self.dimension if self.model is not None else None,
            "query_prompts": self.cfg.backend == "drama" or bool(self.cfg.prompt_tables or self.cfg.prompt_columns),
        }
