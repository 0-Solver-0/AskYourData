-- ══════════════════════════════════════════════════════════════════════════════
--  AskYourData — schéma de la base NL2SQL (pgvector)
--  Source unique de vérité, appliquée par schema_service à chaque démarrage.
--  Toutes les instructions sont idempotentes : rejouer ce script ne casse rien
--  et met à niveau une base créée par une version précédente.
-- ══════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Comptes (multi-tenant) ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nl2sql_accounts (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(200) NOT NULL,
    pg_host    VARCHAR(200),
    pg_port    INT DEFAULT 5432,
    pg_db      VARCHAR(200),
    pg_user    VARCHAR(200),
    pg_schema  VARCHAR(100) DEFAULT 'public',
    created_at TIMESTAMP DEFAULT NOW()
);

-- ── Tables indexées ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nl2sql_tables (
    id          SERIAL PRIMARY KEY,
    account_id  INT NOT NULL REFERENCES nl2sql_accounts(id) ON DELETE CASCADE,
    schema_name VARCHAR(100) NOT NULL,
    table_name  VARCHAR(200) NOT NULL,
    ddl         TEXT,
    description TEXT,
    label       VARCHAR(200),
    synonyms    TEXT,
    domain      VARCHAR(100),
    notes       TEXT,
    embedding   vector(384),
    indexed_at  TIMESTAMP,
    UNIQUE(account_id, schema_name, table_name)
);

-- v3 : métadonnées Odoo, volume, suivi de l'enrichissement, priorité au manuel
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS odoo_model      VARCHAR(200);
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS odoo_label      TEXT;
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS row_count       BIGINT;
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS enriched        BOOLEAN DEFAULT FALSE;
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS enriched_by     VARCHAR(200);
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS enriched_at     TIMESTAMP;
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS manual_override BOOLEAN DEFAULT FALSE;
-- v6 : module Odoo qui définit le modèle (export par module pour l'enrichissement)
ALTER TABLE nl2sql_tables ADD COLUMN IF NOT EXISTS odoo_module     VARCHAR(100);

-- ── Colonnes indexées ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nl2sql_columns (
    id          SERIAL PRIMARY KEY,
    table_id    INT NOT NULL REFERENCES nl2sql_tables(id) ON DELETE CASCADE,
    column_name VARCHAR(200) NOT NULL,
    data_type   VARCHAR(100),
    is_nullable BOOLEAN DEFAULT TRUE,
    is_pk       BOOLEAN DEFAULT FALSE,
    is_fk       BOOLEAN DEFAULT FALSE,
    label       VARCHAR(200),
    synonyms    TEXT,
    example     TEXT,
    notes       TEXT,
    display     BOOLEAN DEFAULT TRUE,
    UNIQUE(table_id, column_name)
);

-- v3 : métadonnées Odoo et embedding stocké (plus de ré-encodage à chaque question)
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS odoo_label       TEXT;
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS odoo_help        TEXT;
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS odoo_type        VARCHAR(50);
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS relation_model   VARCHAR(200);
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS selection_values TEXT;
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS manual_override  BOOLEAN DEFAULT FALSE;
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS embedding        vector(384);
-- v7 : colonne toujours envoyée quand sa table est retenue (statut, type de document…)
ALTER TABLE nl2sql_columns ADD COLUMN IF NOT EXISTS always_include   BOOLEAN DEFAULT FALSE;

-- ── Relations (clés étrangères) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nl2sql_relations (
    id          SERIAL PRIMARY KEY,
    account_id  INT NOT NULL REFERENCES nl2sql_accounts(id) ON DELETE CASCADE,
    from_table  VARCHAR(200) NOT NULL,
    from_column VARCHAR(200) NOT NULL,
    to_table    VARCHAR(200) NOT NULL,
    to_column   VARCHAR(200) NOT NULL,
    label       TEXT,
    join_hint   TEXT,
    UNIQUE(account_id, from_table, from_column, to_table, to_column)
);

-- ── Tables exclues à l'indexation, avec la raison (relecture des filtres) ─────
CREATE TABLE IF NOT EXISTS nl2sql_index_exclusions (
    id          SERIAL PRIMARY KEY,
    account_id  INT NOT NULL REFERENCES nl2sql_accounts(id) ON DELETE CASCADE,
    table_name  VARCHAR(200) NOT NULL,
    reason      VARCHAR(200) NOT NULL,
    recorded_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, table_name)
);

-- ── Valeurs de référence (noms de pays, d'étapes, de clients…) ───────────────
-- Permettent de reconnaître une valeur citée dans la question (« Morocco » → res_country).
CREATE TABLE IF NOT EXISTS nl2sql_values (
    id          SERIAL PRIMARY KEY,
    account_id  INT NOT NULL REFERENCES nl2sql_accounts(id) ON DELETE CASCADE,
    table_name  VARCHAR(200) NOT NULL,
    column_name VARCHAR(200) NOT NULL,
    value       TEXT NOT NULL,
    normalized  TEXT NOT NULL,
    UNIQUE(account_id, table_name, column_name, value)
);

-- v5 : informations sur l'index (modèle d'embedding utilisé, dimension)
CREATE TABLE IF NOT EXISTS nl2sql_meta (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- v8 : plusieurs vecteurs par table et par colonne (libellé, puis un par synonyme) ;
-- le score d'une table ou d'une colonne est le meilleur de ses vecteurs.
-- column_id NULL : vecteur de la table elle-même. source : label | synonym | card (fiche de secours).
-- La colonne embedding de nl2sql_tables / nl2sql_columns garde le premier vecteur (secours).
CREATE TABLE IF NOT EXISTS nl2sql_vectors (
    id         SERIAL PRIMARY KEY,
    table_id   INT NOT NULL REFERENCES nl2sql_tables(id) ON DELETE CASCADE,
    column_id  INT REFERENCES nl2sql_columns(id) ON DELETE CASCADE,
    source     VARCHAR(20) NOT NULL,
    text       TEXT NOT NULL,
    embedding  vector(384)
);
CREATE INDEX IF NOT EXISTS idx_nl2sql_vectors_table  ON nl2sql_vectors(table_id) WHERE column_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_nl2sql_vectors_column ON nl2sql_vectors(column_id);

-- ── Index ─────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_nl2sql_tables_embedding
    ON nl2sql_tables USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_nl2sql_columns_embedding
    ON nl2sql_columns USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_nl2sql_tables_account    ON nl2sql_tables(account_id);
CREATE INDEX IF NOT EXISTS idx_nl2sql_columns_table     ON nl2sql_columns(table_id);
CREATE INDEX IF NOT EXISTS idx_nl2sql_relations_account ON nl2sql_relations(account_id);
CREATE INDEX IF NOT EXISTS idx_nl2sql_values_account    ON nl2sql_values(account_id);

-- ── Compte de démo Odoo (créé une seule fois) ─────────────────────────────────
INSERT INTO nl2sql_accounts (name, pg_host, pg_port, pg_db, pg_user, pg_schema)
SELECT 'Odoo Demo', 'postgres_odoo', 5432, 'odoo', 'odoo', 'public'
WHERE NOT EXISTS (SELECT 1 FROM nl2sql_accounts);
