"""
Crée (ou met à jour) le rôle PostgreSQL en lecture seule utilisé pour exécuter les requêtes
générées, sur la base Odoo. À relancer sans risque (idempotent), par exemple après
l'installation d'un module Odoo (nouvelles tables).

Le rôle :
  - LOGIN, sans aucun autre droit que SELECT sur le schéma public ;
  - default_transaction_read_only = on et statement_timeout par défaut (15 s) ;
  - aucun accès aux tables sensibles (ir_config_parameter, clés d'API…) ;
  - aucun accès aux colonnes sensibles (res_users.password, secrets, jetons…) : droits
    accordés colonne par colonne sur ces tables.

Lit le .env du dossier courant : SOURCE_DB_HOST/PORT/NAME, SOURCE_DB_USER + ODOO_DB_PASSWORD
(administrateur), EXEC_DB_USER + EXEC_DB_PASSWORD (rôle à créer).

Usage (PowerShell, depuis le dossier du projet) :
  docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c
    "pip install -q psycopg2-binary && python tools/create_readonly_role.py"
"""

import os
import re
import sys

import psycopg2
from psycopg2 import sql

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from api_service.sensitive import FORBIDDEN_TABLES, SENSITIVE_COLUMN  # noqa: E402


def read_env(path: str = ".env") -> dict:
    """Lecture simple du .env (commentaires de fin de ligne retirés), l'environnement prime."""
    values = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if value[:1] in "\"'" and value[-1:] == value[:1]:
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            values[key.strip()] = value
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(("SOURCE_", "EXEC_", "ODOO_"))})
    return values


def main() -> int:
    env = read_env()
    role, password = env.get("EXEC_DB_USER") or "askyourdata", env.get("EXEC_DB_PASSWORD") or ""
    if not password:
        print("✗ EXEC_DB_PASSWORD est vide dans .env : choisis un mot de passe, puis relance.")
        return 1
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        print(f"✗ EXEC_DB_USER invalide : {role}")
        return 1
    host, port = env.get("SOURCE_DB_HOST") or "postgres_odoo", int(env.get("SOURCE_DB_PORT") or 5432)
    dbname = env.get("SOURCE_DB_NAME") or "odoo"
    admin = env.get("SOURCE_DB_USER") or "odoo"
    conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=admin,
                            password=env.get("ODOO_DB_PASSWORD") or "odoo")
    conn.autocommit = True
    r = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone():
            cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(r), (password,))
            print(f"• rôle {role} existant : mot de passe mis à jour")
        else:
            cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                "NOINHERIT").format(r), (password,))
            print(f"✓ rôle {role} créé")
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(r))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(r))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), r))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(r))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}")
                    .format(sql.Identifier(admin), r))

        for table in sorted(FORBIDDEN_TABLES):
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0]:
                cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), r))
                print(f"  table interdite : {table}")

        cur.execute("""SELECT c.table_name, array_agg(c.column_name::text ORDER BY c.ordinal_position)
                       FROM information_schema.columns c
                       JOIN information_schema.tables t
                         ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                       WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
                       GROUP BY c.table_name""")
        for table, columns in cur.fetchall():
            sensitive = [c for c in columns if SENSITIVE_COLUMN.search(c)]
            if not sensitive or table in FORBIDDEN_TABLES:
                continue
            allowed = [c for c in columns if c not in sensitive]
            cur.execute(sql.SQL("REVOKE SELECT ON {} FROM {}").format(sql.Identifier(table), r))
            if allowed:
                cur.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                    sql.SQL(", ").join(map(sql.Identifier, allowed)), sql.Identifier(table), r))
            print(f"  colonnes masquées : {table}.{', '.join(sensitive)}")
    conn.close()

    # Vérification avec le rôle lui-même
    check = psycopg2.connect(host=host, port=port, dbname=dbname, user=role, password=password)
    ok = True
    with check.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        read_only = cur.fetchone()[0] == "on"
        print(("✓" if read_only else "✗") + " transactions en lecture seule par défaut")
        ok &= read_only
        for label, query in (("lecture res_partner", "SELECT count(*) FROM res_partner"),
                             ("mot de passe res_users", "SELECT password FROM res_users LIMIT 1"),
                             ("écriture", "CREATE TABLE askyourdata_probe (id int)")):
            try:
                cur.execute(query)
                allowed = True
            except psycopg2.Error:
                allowed = False
            check.rollback()
            expected = label == "lecture res_partner"
            print(("✓" if allowed == expected else "✗") + f" {label} : {'permis' if allowed else 'refusé'}")
            ok &= allowed == expected
    check.close()
    print("Rôle prêt." if ok else "Vérification en échec : voir ci-dessus.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
