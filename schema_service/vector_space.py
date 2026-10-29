"""
schema_service/vector_space.py
------------------------------
Accorde la base NL2SQL avec l'embedder chargé, au démarrage de schema_service.

- Dimension différente (ex. MiniLM 384 → Qwen3-Embedding 1024) : les colonnes `embedding`
  changent de type, les vecteurs sont vidés et les index HNSW recréés.
- Même dimension mais autre modèle (ex. MiniLM → bge-small, tous deux 384) : les vecteurs
  sont vidés, car des vecteurs de deux modèles ne sont pas comparables.
Dans les deux cas, il faut relancer l'indexation (/index) : rien d'autre n'est perdu
(annotations, descriptions, valeurs et relations sont conservés).
"""

import logging

logger = logging.getLogger("schema_service.vector_space")

VECTOR_TABLES = ("nl2sql_tables", "nl2sql_columns")
LEGACY_MODEL_ID = "all-MiniLM-L6-v2"


def current_dimension(conn, table: str = "nl2sql_tables") -> int:
    """Dimension déclarée de la colonne embedding (typmod de pgvector)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid = %s::regclass AND attname = 'embedding' AND NOT attisdropped
        """, (table,))
        row = cur.fetchone()
    return int(row[0]) if row else -1


def get_meta(conn, key: str):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM nl2sql_meta WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO nl2sql_meta (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value))


def missing_embeddings(conn) -> int:
    """Tables indexées sans vecteur : > 0 signifie qu'il faut relancer /index."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM nl2sql_tables WHERE embedding IS NULL")
        return int(cur.fetchone()[0])


def ensure_vector_space(conn, dimension: int, model_id: str) -> dict:
    """Retourne {"dimension_changed", "model_changed", "reindex_required"}."""
    changed_dim = False
    for table in VECTOR_TABLES:
        if current_dimension(conn, table) != dimension:
            changed_dim = True
    with conn.cursor() as cur:
        if changed_dim:
            logger.warning("Dimension des vecteurs → %d : vecteurs vidés, réindexation nécessaire", dimension)
            for table in VECTOR_TABLES:
                cur.execute(f"DROP INDEX IF EXISTS idx_{table}_embedding")
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({int(dimension)}) USING NULL")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_embedding "
                            f"ON {table} USING hnsw (embedding vector_cosine_ops)")

    previous = get_meta(conn, "embedder_model")
    if previous is None and not changed_dim:
        # Index créé avant l'enregistrement du modèle : c'était forcément MiniLM
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM nl2sql_tables WHERE embedding IS NOT NULL)")
            if cur.fetchone()[0]:
                previous = LEGACY_MODEL_ID
    model_changed = previous is not None and previous != model_id
    if model_changed and not changed_dim:
        logger.warning("Modèle d'embedding %s → %s : vecteurs vidés, réindexation nécessaire", previous, model_id)
        with conn.cursor() as cur:
            for table in VECTOR_TABLES:
                cur.execute(f"UPDATE {table} SET embedding = NULL WHERE embedding IS NOT NULL")
    set_meta(conn, "embedder_model", model_id)
    set_meta(conn, "embedder_dimension", str(dimension))
    conn.commit()
    return {
        "dimension_changed": changed_dim,
        "model_changed": model_changed,
        "reindex_required": missing_embeddings(conn) > 0,
    }
