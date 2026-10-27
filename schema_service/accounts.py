"""
schema_service/accounts.py
--------------------------
Compte par défaut (id=1) : sa connexion à la base source vient de la configuration (.env),
et non d'une modification manuelle de la base NL2SQL.
"""

import os

import psycopg2.extras


def default_account_from_env() -> dict:
    """Paramètres du compte par défaut (id=1) lus dans l'environnement."""
    return {
        "pg_host":   os.getenv("SOURCE_DB_HOST", "postgres_odoo"),
        "pg_port":   int(os.getenv("SOURCE_DB_PORT", "5432")),
        "pg_db":     os.getenv("SOURCE_DB_NAME", "odoo"),
        "pg_user":   os.getenv("SOURCE_DB_USER", "odoo"),
        "pg_schema": os.getenv("SOURCE_DB_SCHEMA", "public"),
    }


def sync_default_account(conn, params: dict, account_id: int = 1) -> dict:
    """
    Aligne le compte par défaut sur la configuration (.env) : crée le compte s'il manque,
    sinon met à jour sa connexion. Les autres comptes ne sont pas touchés.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO nl2sql_accounts (id, name, pg_host, pg_port, pg_db, pg_user, pg_schema)
            VALUES (%(id)s, 'Odoo Demo', %(pg_host)s, %(pg_port)s, %(pg_db)s, %(pg_user)s, %(pg_schema)s)
            ON CONFLICT (id) DO UPDATE SET
                pg_host   = EXCLUDED.pg_host,
                pg_port   = EXCLUDED.pg_port,
                pg_db     = EXCLUDED.pg_db,
                pg_user   = EXCLUDED.pg_user,
                pg_schema = EXCLUDED.pg_schema
            RETURNING id, name, pg_host, pg_port, pg_db, pg_user, pg_schema
        """, {**params, "id": account_id})
        row = dict(cur.fetchone())
        # La séquence doit rester au-delà de l'id inséré explicitement
        cur.execute("SELECT setval(pg_get_serial_sequence('nl2sql_accounts', 'id'), "
                    "GREATEST((SELECT MAX(id) FROM nl2sql_accounts), 1))")
    conn.commit()
    return row
