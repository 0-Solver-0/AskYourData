-- ══════════════════════════════════════════════════════════════════════════════
--  AskYourData — initialisation de postgres_nl2sql
--  Exécuté uniquement à la création du volume.
--
--  Le schéma complet (tables nl2sql_*, index, compte de démo) est appliqué par
--  schema_service à chaque démarrage, depuis schema_service/schema.sql : il est
--  idempotent et met aussi à niveau une base existante.
-- ══════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;
