"""Tests de la configuration de l'embedder (sans modèle ni torch)."""

import pytest

from schema_service.embedder import QWEN3_PROMPTS, Embedder, EmbedderConfig, resolve_device

ENV_KEYS = ("EMBEDDER_MODEL_PATH", "MINILM_MODEL_PATH", "EMBEDDER_DEVICE", "EMBEDDER_DIM",
            "EMBEDDER_PROMPT_TABLES", "EMBEDDER_PROMPT_COLUMNS", "EMBEDDER_FP16", "EMBEDDER_BATCH_SIZE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_is_minilm_without_prompts():
    cfg = EmbedderConfig.from_env()
    assert cfg.model_path.endswith("all-MiniLM-L6-v2")
    assert cfg.model_id == "all-MiniLM-L6-v2"
    assert cfg.prompt_tables == "" and cfg.prompt_columns == ""
    assert cfg.device == "auto" and cfg.dim is None


def test_legacy_minilm_variable_is_still_read(monkeypatch):
    monkeypatch.setenv("MINILM_MODEL_PATH", "/models/custom")
    assert EmbedderConfig.from_env().model_path == "/models/custom"


def test_qwen_gets_its_instructions_and_dimension(monkeypatch):
    monkeypatch.setenv("EMBEDDER_MODEL_PATH", "/app/models/Qwen3-Embedding-0.6B/")
    monkeypatch.setenv("EMBEDDER_DIM", "512")
    cfg = EmbedderConfig.from_env()
    assert cfg.prompt_tables == QWEN3_PROMPTS["tables"]
    assert cfg.prompt_columns.startswith("Instruct: ") and cfg.prompt_columns.endswith("\nQuery:")
    assert cfg.model_id == "Qwen3-Embedding-0.6B@512"


def test_explicit_empty_prompt_overrides_default(monkeypatch):
    monkeypatch.setenv("EMBEDDER_MODEL_PATH", "/app/models/Qwen3-Embedding-0.6B")
    monkeypatch.setenv("EMBEDDER_PROMPT_TABLES", "")
    cfg = EmbedderConfig.from_env()
    assert cfg.prompt_tables == ""
    assert cfg.prompt_columns == QWEN3_PROMPTS["columns"]


def test_unknown_device_is_rejected(monkeypatch):
    monkeypatch.setenv("EMBEDDER_DEVICE", "tpu")
    with pytest.raises(ValueError):
        EmbedderConfig.from_env()


def test_resolve_device():
    assert resolve_device("auto", True) == "cuda"
    assert resolve_device("auto", False) == "cpu"
    assert resolve_device("cpu", True) == "cpu"
    assert resolve_device("cuda", False) == "cpu"     # repli sur CPU, avec un avertissement


class FakeModel:
    def __init__(self):
        self.calls = []

    def encode(self, texts, batch_size=32, prompt=None, show_progress_bar=False):
        self.calls.append((list(texts), prompt))
        return [[float(len(t)), 0.5] for t in texts]

    def get_sentence_embedding_dimension(self):
        return 2


def test_documents_have_no_prompt_and_queries_do():
    cfg = EmbedderConfig(model_path="/m/Qwen3-Embedding-0.6B", prompt_tables="T:", prompt_columns="C:")
    model = FakeModel()
    emb = Embedder(cfg, model=model)
    assert emb.embed_documents(["sale_order"]) == [[10.0, 0.5]]
    emb.embed_query("Top customers", "tables")
    emb.embed_queries(["units in stock"], "columns")
    assert [p for _, p in model.calls] == [None, "T:", "C:"]
    assert emb.embed_queries([], "columns") == []
    assert emb.info()["dimension"] == 2 and emb.info()["query_prompts"] is True
