-- ══════════════════════════════════════════════════════════════════════════════
--  NL2SQL — init.sql
--  Exécuté automatiquement au premier démarrage de postgres_nl2sql
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

-- ── Tables annotées + embeddings ─────────────────────────────────────────────
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

-- ── Colonnes annotées ─────────────────────────────────────────────────────────
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

-- ── Relations annotées ────────────────────────────────────────────────────────
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

-- ── Index HNSW ────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_nl2sql_tables_embedding
ON nl2sql_tables USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_nl2sql_tables_account    ON nl2sql_tables(account_id);
CREATE INDEX IF NOT EXISTS idx_nl2sql_columns_table     ON nl2sql_columns(table_id);
CREATE INDEX IF NOT EXISTS idx_nl2sql_relations_account ON nl2sql_relations(account_id);

-- ── Compte de démo Odoo ───────────────────────────────────────────────────────
INSERT INTO nl2sql_accounts (name, pg_host, pg_port, pg_db, pg_user, pg_schema)
VALUES ('Odoo Demo', 'postgres_odoo', 5432, 'odoo', 'odoo', 'public')
ON CONFLICT DO NOTHING;
